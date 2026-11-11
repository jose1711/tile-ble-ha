"""Tile BLE (local): recognizes Tile trackers by the privateId derived from authKey."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher, BluetoothChange, BluetoothScanningMode, BluetoothServiceInfoBleak)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval

from .const import (BATTERY_INTERVAL_HOURS, CONF_BATTERY_ENABLED, CONF_EMAIL, CONF_PASSWORD, CONF_TILES,
                    SIGNAL_BATTERY, SIGNAL_SEEN, TILE_SERVICE_UUID)
from .privateid import extract_private_id, private_ids
from .tile_cloud import TileAuthError, TileRequestError, fetch_battery_statuses

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["binary_sensor", "sensor", "button"]


@dataclass
class Sighting:
    last_seen: float  # time.monotonic()
    rssi: int
    address: str
    source: str
    ctr: int


@dataclass
class TileBleData:
    tiles: dict[str, dict]                      # tile_uuid -> {name, product, ...}
    table: dict[bytes, tuple[str, int]]         # privateId -> (tile_uuid, ctr)
    seen: dict[str, Sighting] = field(default_factory=dict)
    started: float = field(default_factory=time.monotonic)  # setup time (monotonic)
    battery: dict[str, str] = field(default_factory=dict)  # tile_uuid -> "healthy"|"low"|"dead"|"unknown"


type TileBleConfigEntry = ConfigEntry[TileBleData]


def _build_table(tiles: list[dict]) -> dict[bytes, tuple[str, int]]:
    table: dict[bytes, tuple[str, int]] = {}
    for t in tiles:
        for pid, ctr in private_ids(t["auth_key"], t["tile_uuid"]).items():
            table[pid] = (t["tile_uuid"], ctr)
    return table


async def async_setup_entry(hass: HomeAssistant, entry: TileBleConfigEntry) -> bool:
    tiles = entry.data[CONF_TILES]
    table = await hass.async_add_executor_job(_build_table, tiles)
    data = TileBleData(tiles={t["tile_uuid"]: t for t in tiles}, table=table)
    entry.runtime_data = data
    signal = SIGNAL_SEEN.format(entry_id=entry.entry_id)

    @callback
    def _process(info: BluetoothServiceInfoBleak, seen_at: float | None = None) -> None:
        sd = info.service_data.get(TILE_SERVICE_UUID)
        pid = extract_private_id(sd) if sd else None
        if pid is None or (hit := table.get(pid)) is None:
            return
        tile_uuid, ctr = hit
        last_seen = time.monotonic() if seen_at is None else seen_at
        prev = data.seen.get(tile_uuid)
        if prev is not None and prev.last_seen > last_seen:
            return
        data.seen[tile_uuid] = Sighting(last_seen, info.rssi, info.address, info.source, ctr)
        async_dispatcher_send(hass, signal, tile_uuid)

    @callback
    def _adv(info: BluetoothServiceInfoBleak, change: BluetoothChange) -> None:
        _process(info)

    # seed from advertisements HA already knows about (e.g. after an entry reload)
    for known in bluetooth.async_discovered_service_info(hass, connectable=False):
        _process(known, known.time)

    entry.async_on_unload(bluetooth.async_register_callback(
        hass, _adv,
        BluetoothCallbackMatcher(service_uuid=TILE_SERVICE_UUID, connectable=False),
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

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _reload(hass: HomeAssistant, entry: TileBleConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: TileBleConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
