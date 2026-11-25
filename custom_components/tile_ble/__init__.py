"""Tile BLE (local): recognizes Tile trackers by the privateId derived from authKey (plus optional iBeacons)."""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta

import voluptuous as vol

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher, BluetoothChange, BluetoothScanningMode, BluetoothServiceInfoBleak)
from homeassistant.components.zone import ENTITY_ID_HOME as ZONE_HOME
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_GPS_ACCURACY, ATTR_LATITUDE, ATTR_LONGITUDE
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval

from . import ibeacon
from .const import (BATTERY_INTERVAL_HOURS, CONF_BATTERY_ENABLED, CONF_EMAIL, CONF_IBEACONS, CONF_PASSWORD,
                    CONF_TILES, DOMAIN, SERVICE_REPORT_ADVERTISEMENT, SERVICE_REPORT_IBEACON, SIGNAL_BATTERY,
                    SIGNAL_SEEN, TILE_SERVICE_UUID)
from .ibeacon import normalize_uuid
from .privateid import extract_private_id, private_ids
from .tile_cloud import TileAuthError, TileRequestError, fetch_battery_statuses

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["binary_sensor", "sensor", "button", "device_tracker"]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# where the scanner was; shared by report_advertisement and report_ibeacon
_SCANNER_FIELDS = {
    vol.Optional("rssi", default=-127): vol.Coerce(int),
    vol.Optional("address", default=""): cv.string,
    vol.Optional("source", default="remote"): cv.string,
    vol.Optional("zone", default=ZONE_HOME): cv.entity_domain("zone"),  # static zone of that scanner
    # a mobile scanner (phone): where it was when it heard the tracker; takes precedence over zone
    vol.Inclusive("latitude", "coordinates"): cv.latitude,
    vol.Inclusive("longitude", "coordinates"): cv.longitude,
    vol.Optional("gps_accuracy", default=0): vol.All(vol.Coerce(float), vol.Range(min=0)),
    # ...or take the coordinates from an existing tracker (e.g. the HA Companion app of that phone)
    vol.Optional("source_tracker"): cv.entity_domain(["device_tracker", "person"]),
}
REPORT_SCHEMA = vol.Schema({
    vol.Required("service_data"): cv.string,  # hex of the 0xFEED service data
    **_SCANNER_FIELDS,
})
_U16 = vol.All(vol.Coerce(int), vol.Range(min=0, max=0xFFFF))
REPORT_IBEACON_SCHEMA = vol.Schema({
    vol.Required("uuid"): cv.string,
    vol.Required("major"): _U16,
    vol.Required("minor"): _U16,
    **_SCANNER_FIELDS,
})


@dataclass(frozen=True)
class Location:
    latitude: float
    longitude: float
    accuracy: float = 0


@dataclass
class Sighting:
    last_seen: float  # time.monotonic()
    rssi: int
    address: str
    source: str
    ctr: int  # Tile privateId counter; for iBeacons the minor
    zone: str = ZONE_HOME  # static zone of the scanner that heard it
    location: Location | None = None  # coordinates of a mobile scanner; when set, zone is ignored
    wall: float = field(default_factory=time.time)  # wall-clock time of the sighting (for last_seen)


@dataclass
class TileBleData:
    tiles: dict[str, dict]                      # tile_uuid -> {name, product, ...}
    table: dict[bytes, tuple[str, int]]         # privateId -> (tile_uuid, ctr)
    beacons: dict[str, dict] = field(default_factory=dict)  # beacon key -> {name, uuid, major, minor, ...}
    seen: dict[str, Sighting] = field(default_factory=dict)  # tile_uuid / beacon key -> last sighting
    started: float = field(default_factory=time.monotonic)  # setup time (monotonic)
    battery: dict[str, str] = field(default_factory=dict)  # tile_uuid -> "healthy"|"low"|"dead"|"unknown"
    # (service_data, rssi, address, source, seen_at, zone, location) -> matched; set in async_setup_entry
    process: Callable[[bytes, int, str, str, float | None, str, Location | None], bool] | None = None
    # (uuid, major, minor, rssi, address, source, seen_at, zone, location) -> matched
    process_ibeacon: Callable[..., bool] | None = None

    @property
    def trackables(self) -> dict[str, dict]:
        """Tiles and iBeacons: everything that gets presence/RSSI/device_tracker entities."""
        return {**self.tiles, **self.beacons}


type TileBleConfigEntry = ConfigEntry[TileBleData]


def _build_table(tiles: list[dict]) -> dict[bytes, tuple[str, int]]:
    table: dict[bytes, tuple[str, int]] = {}
    for t in tiles:
        for pid, ctr in private_ids(t["auth_key"], t["tile_uuid"]).items():
            table[pid] = (t["tile_uuid"], ctr)
    return table


def _resolve_location(hass: HomeAssistant, data: dict) -> Location | None:
    """Coordinates of the reporting scanner: explicit latitude/longitude, else source_tracker's, else None."""
    if "latitude" in data:
        return Location(data["latitude"], data["longitude"], data["gps_accuracy"])
    if (tracker := data.get("source_tracker")) is None:
        return None
    if (state := hass.states.get(tracker)) is None:
        raise ServiceValidationError(f"source_tracker {tracker} does not exist")
    attrs = state.attributes
    lat, lon = attrs.get(ATTR_LATITUDE), attrs.get(ATTR_LONGITUDE)
    if lat is None or lon is None:
        _LOGGER.debug("%s has no coordinates, falling back to zone %s", tracker, data["zone"])
        return None
    return Location(float(lat), float(lon), float(attrs.get(ATTR_GPS_ACCURACY) or 0))


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    async def _report(call: ServiceCall) -> None:
        """Feed a Tile advertisement captured outside HA (a Linux box running bleak, a phone)."""
        try:
            sd = bytes.fromhex(call.data["service_data"])
        except ValueError as err:
            raise ServiceValidationError(f"service_data is not valid hex: {err}") from err
        location = _resolve_location(hass, call.data)
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            entry.runtime_data.process(sd, call.data["rssi"], call.data["address"], call.data["source"], None,
                                       call.data["zone"], location)

    async def _report_ibeacon(call: ServiceCall) -> None:
        """Feed an iBeacon advertisement captured outside HA."""
        try:
            uuid = normalize_uuid(call.data["uuid"])
        except ValueError as err:
            raise ServiceValidationError(f"uuid is not a valid UUID: {err}") from err
        location = _resolve_location(hass, call.data)
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            entry.runtime_data.process_ibeacon(
                uuid, call.data["major"], call.data["minor"], call.data["rssi"], call.data["address"],
                call.data["source"], None, call.data["zone"], location)

    hass.services.async_register(DOMAIN, SERVICE_REPORT_ADVERTISEMENT, _report, schema=REPORT_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_REPORT_IBEACON, _report_ibeacon, schema=REPORT_IBEACON_SCHEMA)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: TileBleConfigEntry) -> bool:
    tiles = entry.data[CONF_TILES]
    table = await hass.async_add_executor_job(_build_table, tiles)
    try:
        beacons = ibeacon.parse_config(entry.options.get(CONF_IBEACONS, ""))
    except ibeacon.IBeaconConfigError as err:  # validated in the options flow; only a hand-edited entry
        _LOGGER.error("Ignoring the iBeacon list: %s", err)
        beacons = []
    data = TileBleData(tiles={t["tile_uuid"]: t for t in tiles}, table=table,
                       beacons={b["tile_uuid"]: b for b in beacons})
    beacon_lookup = ibeacon.build_lookup(beacons)
    entry.runtime_data = data
    signal = SIGNAL_SEEN.format(entry_id=entry.entry_id)

    @callback
    def _record(key: str, ctr: int, rssi: int, address: str, source: str, seen_at: float | None,
                zone: str, location: Location | None) -> None:
        now = time.monotonic()
        last_seen = now if seen_at is None else seen_at
        prev = data.seen.get(key)
        if prev is not None and prev.last_seen > last_seen:
            return
        data.seen[key] = Sighting(last_seen, rssi, address, source, ctr, zone, location,
                                  time.time() - (now - last_seen))
        async_dispatcher_send(hass, signal, key)

    @callback
    def _process_raw(sd: bytes | None, rssi: int, address: str, source: str,
                     seen_at: float | None, zone: str = ZONE_HOME, location: Location | None = None) -> bool:
        pid = extract_private_id(sd) if sd else None
        if pid is None or (hit := table.get(pid)) is None:
            return False
        tile_uuid, ctr = hit
        _record(tile_uuid, ctr, rssi, address, source, seen_at, zone, location)
        return True

    @callback
    def _process_ibeacon(uuid: str, major: int, minor: int, rssi: int, address: str, source: str,
                         seen_at: float | None, zone: str = ZONE_HOME, location: Location | None = None) -> bool:
        if (key := ibeacon.match(beacon_lookup, uuid, major, minor)) is None:
            return False
        _record(key, minor, rssi, address, source, seen_at, zone, location)
        return True

    data.process = _process_raw
    data.process_ibeacon = _process_ibeacon

    @callback
    def _process(info: BluetoothServiceInfoBleak, seen_at: float | None = None) -> None:
        _process_raw(info.service_data.get(TILE_SERVICE_UUID), info.rssi, info.address, info.source, seen_at)

    @callback
    def _process_beacon(info: BluetoothServiceInfoBleak, seen_at: float | None = None) -> None:
        md = info.manufacturer_data.get(ibeacon.APPLE_COMPANY_ID)
        if md and (adv := ibeacon.parse_advertisement(md)) is not None:
            _process_ibeacon(adv[0], adv[1], adv[2], info.rssi, info.address, info.source, seen_at)

    @callback
    def _adv(info: BluetoothServiceInfoBleak, change: BluetoothChange) -> None:
        _process(info)

    @callback
    def _beacon_adv(info: BluetoothServiceInfoBleak, change: BluetoothChange) -> None:
        _process_beacon(info)

    # seed from advertisements HA already knows about (e.g. after an entry reload)
    for known in bluetooth.async_discovered_service_info(hass, connectable=False):
        _process(known, known.time)
        if beacons:
            _process_beacon(known, known.time)

    entry.async_on_unload(bluetooth.async_register_callback(
        hass, _adv,
        BluetoothCallbackMatcher(service_uuid=TILE_SERVICE_UUID, connectable=False),
        BluetoothScanningMode.PASSIVE))
    if beacons:
        entry.async_on_unload(bluetooth.async_register_callback(
            hass, _beacon_adv,
            BluetoothCallbackMatcher(manufacturer_id=ibeacon.APPLE_COMPANY_ID, connectable=False),
            BluetoothScanningMode.PASSIVE))
    entry.async_on_unload(entry.add_update_listener(_reload))

    if entry.data.get(CONF_BATTERY_ENABLED):
        async def _refresh_battery(_now=None) -> None:
            try:
                statuses = await fetch_battery_statuses(
                    async_get_clientsession(hass), entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD],
                    list(data.tiles))
            except TileAuthError:
                _LOGGER.warning("Tile cloud rejected the stored credentials - reauthentication needed")
                entry.async_start_reauth(hass)
                return
            except TileRequestError as err:
                _LOGGER.warning("Failed to fetch battery_status from the Tile cloud: %s", err)
                return
            data.battery.update(statuses)
            async_dispatcher_send(hass, SIGNAL_BATTERY.format(entry_id=entry.entry_id))

        entry.async_on_unload(async_track_time_interval(
            hass, _refresh_battery, timedelta(hours=BATTERY_INTERVAL_HOURS)))
        entry.async_create_background_task(hass, _refresh_battery(), "tile_ble battery refresh")

    _remove_stale_beacon_devices(hass, entry, data)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _remove_stale_beacon_devices(hass: HomeAssistant, entry: TileBleConfigEntry, data: TileBleData) -> None:
    """Drop devices (and their entities) of iBeacons that were removed from the Options list."""
    reg = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(reg, entry.entry_id):
        keys = {i[1] for i in device.identifiers if i[0] == DOMAIN}
        if any(k.startswith("ibeacon_") for k in keys) and not keys & data.beacons.keys():
            reg.async_update_device(device.id, remove_config_entry_id=entry.entry_id)


async def _reload(hass: HomeAssistant, entry: TileBleConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: TileBleConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
