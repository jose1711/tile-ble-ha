"""Tile BLE (local): recognizes Tile trackers by the privateId derived from authKey (plus optional iBeacons)."""
from __future__ import annotations

import base64
import logging
import time
from http import HTTPStatus
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta

import voluptuous as vol
from aiohttp import web

from homeassistant.components import bluetooth, webhook
from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher, BluetoothChange, BluetoothScanningMode, BluetoothServiceInfoBleak)
from homeassistant.components.zone import ENTITY_ID_HOME as ZONE_HOME
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_GPS_ACCURACY, ATTR_LATITUDE, ATTR_LONGITUDE, CONF_WEBHOOK_ID
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import ibeacon
from . import toa_protocol as toa
from .const import (BATTERY_INTERVAL_HOURS, CONF_BATTERY_ENABLED, CONF_EMAIL, CONF_IBEACONS, CONF_PASSWORD,
                    CONF_TILES, DOMAIN, SERVICE_REPORT_ADVERTISEMENT, SERVICE_REPORT_IBEACON, SIGNAL_BATTERY,
                    SIGNAL_SEEN, TILE_SERVICE_UUID)
from .ibeacon import normalize_uuid
from .privateid import COUNTER_MAX, extract_private_id, private_ids
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
    # tile_uuid -> (ctr, wall-clock time) of the last sighting; persisted, so the phase survives a restart
    phase: dict[str, tuple[int, float]] = field(default_factory=dict)

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


def _report_tile(hass: HomeAssistant, entries: list[TileBleConfigEntry], data: dict) -> bool:
    """Feed a Tile advertisement captured outside HA (a Linux box running bleak, a phone)."""
    try:
        sd = bytes.fromhex(data["service_data"])
    except ValueError as err:
        raise ServiceValidationError(f"service_data is not valid hex: {err}") from err
    location = _resolve_location(hass, data)
    matched = False
    for entry in entries:
        matched |= entry.runtime_data.process(sd, data["rssi"], data["address"], data["source"], None,
                                              data["zone"], location)
    return matched


def _report_ibeacon(hass: HomeAssistant, entries: list[TileBleConfigEntry], data: dict) -> bool:
    """Feed an iBeacon advertisement captured outside HA."""
    try:
        uuid = normalize_uuid(data["uuid"])
    except ValueError as err:
        raise ServiceValidationError(f"uuid is not a valid UUID: {err}") from err
    location = _resolve_location(hass, data)
    matched = False
    for entry in entries:
        matched |= entry.runtime_data.process_ibeacon(
            uuid, data["major"], data["minor"], data["rssi"], data["address"], data["source"], None,
            data["zone"], location)
    return matched


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    async def _report(call: ServiceCall) -> None:
        _report_tile(hass, hass.config_entries.async_loaded_entries(DOMAIN), call.data)

    async def _report_beacon(call: ServiceCall) -> None:
        _report_ibeacon(hass, hass.config_entries.async_loaded_entries(DOMAIN), call.data)

    hass.services.async_register(DOMAIN, SERVICE_REPORT_ADVERTISEMENT, _report, schema=REPORT_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_REPORT_IBEACON, _report_beacon, schema=REPORT_IBEACON_SCHEMA)
    return True


async def _handle_webhook(hass: HomeAssistant, webhook_id: str, request: web.Request) -> web.Response:
    """Mobile scanner without an HA token: POST {"type": "tile"|"ibeacon"|"ping", ...service fields}.

    The webhook id is the secret; it only allows feeding sightings into this entry.
    """
    entry = next((e for e in hass.config_entries.async_loaded_entries(DOMAIN)
                  if e.data.get(CONF_WEBHOOK_ID) == webhook_id), None)
    if entry is None:
        return web.json_response({"error": "not loaded"}, status=HTTPStatus.SERVICE_UNAVAILABLE)
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "invalid JSON"}, status=HTTPStatus.BAD_REQUEST)
    if not isinstance(body, dict):
        return web.json_response({"error": "expected a JSON object"}, status=HTTPStatus.BAD_REQUEST)
    kind = body.pop("type", None)
    handlers = {"tile": (REPORT_SCHEMA, _report_tile), "ibeacon": (REPORT_IBEACON_SCHEMA, _report_ibeacon)}
    if kind == "trackables":
        try:
            window = vol.Schema({vol.Optional("window_days", default=1): vol.All(
                vol.Coerce(int), vol.Range(min=0, max=90))}, extra=vol.REMOVE_EXTRA)(body)["window_days"]
        except vol.Invalid as err:
            return web.json_response({"error": str(err)}, status=HTTPStatus.BAD_REQUEST)
        return web.json_response(trackables_payload(entry.runtime_data, window, time.time()))
    if kind == "session_key":
        try:
            req = SESSION_KEY_SCHEMA(body)
            return web.json_response(session_key_payload(entry.runtime_data, req))
        except (vol.Invalid, ValueError) as err:
            return web.json_response({"error": str(err)}, status=HTTPStatus.BAD_REQUEST)
    if kind == "ping":
        return web.json_response({"ok": True, "tiles": len(entry.runtime_data.tiles),
                                  "beacons": len(entry.runtime_data.beacons)})
    if kind not in handlers:
        return web.json_response({"error": f"unknown type {kind!r}"}, status=HTTPStatus.BAD_REQUEST)
    schema, handler = handlers[kind]
    try:
        matched = handler(hass, [entry], schema(body))
    except (vol.Invalid, ServiceValidationError) as err:
        return web.json_response({"error": str(err)}, status=HTTPStatus.BAD_REQUEST)
    return web.json_response({"matched": matched})


_HEX = vol.All(cv.string, vol.Match(r"^([0-9a-fA-F]{2})*$"))
SESSION_KEY_SCHEMA = vol.Schema({
    vol.Required("key"): cv.string,               # tile_uuid
    vol.Required("rand_a"): _HEX,                 # 14 B chosen by the phone
    vol.Required("channel_prefix"): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
    vol.Required("channel_data"): _HEX,           # from the tracker's OPEN_CHANNEL response
    vol.Required("local_token"): _HEX,            # 4 B chosen by the phone
}, extra=vol.REMOVE_EXTRA)


def session_key_payload(data: TileBleData, req: dict) -> dict:
    """Key of one authenticated BLE channel, so a phone can ring a Tile without ever getting its auth_key.

    It is HMAC(auth_key, randA | channelData | channelPrefix | localToken)[:16] (see ring_ble), valid only
    for the channel opened with these values.
    """
    if (tile := data.tiles.get(req["key"])) is None:
        raise ValueError(f"unknown tile {req['key']}")
    rand_a, channel_data = bytes.fromhex(req["rand_a"]), bytes.fromhex(req["channel_data"])
    local_token = bytes.fromhex(req["local_token"])
    if len(rand_a) != 14 or len(local_token) != 4:
        raise ValueError("rand_a must be 14 B and local_token 4 B")
    key = toa.generate_hmac(base64.b64decode(tile["auth_key"]), rand_a, channel_data,
                            req["channel_prefix"], local_token)[:16]  # ValueError when > 32 B
    return {"session_key": key.hex(), "expected_tdt_cmd_config": tile.get("expected_tdt_cmd_config")}


CTR_PERIOD = 900  # s; the privateId counter advances every 15 min
CTR_PER_DAY = 86400 // CTR_PERIOD


def trackables_payload(data: TileBleData, window_days: int, now: float) -> dict:
    """What the phone app needs to recognise and name the trackers (never the auth_key).

    Tiles: privateIds (hex) for the current counter +-window_days (+1 day for clock drift) when the
    phase is known from a sighting, else the whole 90-day cycle. valid_until: when to refresh.
    """
    by_tile: dict[str, dict[int, bytes]] = {}
    for pid, (tile_uuid, ctr) in data.table.items():
        by_tile.setdefault(tile_uuid, {})[ctr] = pid
    tiles = []
    for tile_uuid, tile in data.tiles.items():
        ids = by_tile.get(tile_uuid, {})
        valid_until = None
        if window_days and (ph := data.phase.get(tile_uuid)) is not None:
            cur = (ph[0] + int((now - ph[1]) // CTR_PERIOD)) % COUNTER_MAX
            span = (window_days + 1) * CTR_PER_DAY
            ctrs = [(cur + d) % COUNTER_MAX for d in range(-span, span + 1)]
            valid_until = dt_util.utc_from_timestamp(now + window_days * 43200).isoformat()
        else:
            ctrs = sorted(ids)
        tiles.append({"key": tile_uuid, "name": tile.get("name", tile_uuid),
                      "ids": [ids[c].hex() for c in ctrs if c in ids], "valid_until": valid_until})
    beacons = [{"key": k, "name": b["name"], "uuid": b["uuid"], "major": b["major"], "minor": b["minor"]}
               for k, b in data.beacons.items()]
    return {"tiles": tiles, "beacons": beacons}


def webhook_url(hass: HomeAssistant, webhook_id: str) -> str:
    """Full webhook URL for the app; prefers the external URL (the phone is mostly away from home)."""
    try:
        base = get_url(hass, prefer_external=True, allow_cloud=True)
    except NoURLAvailableError:
        return webhook.async_generate_path(webhook_id)
    return base + webhook.async_generate_path(webhook_id)


async def async_setup_entry(hass: HomeAssistant, entry: TileBleConfigEntry) -> bool:
    if CONF_WEBHOOK_ID not in entry.data:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_WEBHOOK_ID: webhook.async_generate_id()})
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
    store: Store[dict] = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.phase")
    data.phase = {k: (int(v[0]), float(v[1])) for k, v in ((await store.async_load()) or {}).items()
                  if k in data.tiles}
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
        if key in data.tiles:
            data.phase[key] = (ctr, data.seen[key].wall)
            store.async_delay_save(lambda: {k: list(v) for k, v in data.phase.items()}, 60)
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

    # the options flow may replace the id; unregister the one registered here
    webhook_id = entry.data[CONF_WEBHOOK_ID]
    webhook.async_register(hass, DOMAIN, "Tile BLE mobile scanner", webhook_id, _handle_webhook,
                           local_only=False, allowed_methods=["POST"])
    entry.async_on_unload(lambda: webhook.async_unregister(hass, webhook_id))

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
