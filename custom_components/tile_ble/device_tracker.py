"""Zone tracker: the tracker is in the static zone of the scanner that heard it last.

HA's own adapters/proxies count as zone.home; external scanners pass their zone to
tile_ble.report_advertisement. No sighting within the presence timeout -> not_home.
"""
from __future__ import annotations

import time
from datetime import timedelta

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_time_interval

from .const import CONF_TIMEOUT, DEFAULT_TIMEOUT, SIGNAL_SEEN
from .entity import TileBleEntity


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities) -> None:
    data = entry.runtime_data
    timeout = entry.options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT) * 60
    async_add_entities(TileZoneTracker(t, data, entry.entry_id, timeout) for t in data.tiles.values())


class TileZoneTracker(TileBleEntity, TrackerEntity):
    _attr_source_type = SourceType.BLUETOOTH_LE
    _attr_name = None  # the device name, e.g. device_tracker.keys

    def __init__(self, tile, data, entry_id, timeout) -> None:
        super().__init__(tile)
        self._data, self._entry_id, self._timeout = data, entry_id, timeout
        self._attr_unique_id = f"{self._tile_uuid}_tracker"

    @callback
    def _refresh(self) -> None:
        now = time.monotonic()
        s = self._data.seen.get(self._tile_uuid)
        if s is None:
            # like the presence sensor: unknown (None) for one timeout window after startup
            zones = None if now - self._data.started < self._timeout else []
        else:
            zones = [s.zone] if now - s.last_seen < self._timeout else []
        self._attr_in_zones = zones
        self._attr_extra_state_attributes = {"source": s.source} if s else {}
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        @callback
        def _seen(tile_uuid: str) -> None:
            if tile_uuid == self._tile_uuid:
                self._refresh()

        self.async_on_remove(async_dispatcher_connect(
            self.hass, SIGNAL_SEEN.format(entry_id=self._entry_id), _seen))
        # must be a @callback (a plain lambda would run in the executor thread)
        @callback
        def _tick(_now) -> None:
            self._refresh()

        self.async_on_remove(async_track_time_interval(self.hass, _tick, timedelta(seconds=30)))
        self._refresh()
