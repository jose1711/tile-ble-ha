"""Presence: the tracker is 'home' if it was seen within the last N minutes."""
from __future__ import annotations

import time
from datetime import timedelta

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_time_interval

from .const import CONF_TIMEOUT, DEFAULT_TIMEOUT, SIGNAL_SEEN
from .entity import TileBleEntity


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities) -> None:
    data = entry.runtime_data
    timeout = entry.options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT) * 60
    async_add_entities(TilePresence(t, data, entry.entry_id, timeout) for t in data.tiles.values())


class TilePresence(TileBleEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.PRESENCE
    _attr_name = "Presence"

    def __init__(self, tile, data, entry_id, timeout) -> None:
        super().__init__(tile)
        self._data, self._entry_id, self._timeout = data, entry_id, timeout
        self._attr_unique_id = f"{self._tile_uuid}_presence"

    @property
    def is_on(self) -> bool | None:
        now = time.monotonic()
        s = self._data.seen.get(self._tile_uuid)
        if s is None:
            # Nothing heard yet (e.g. just after an HA restart): stay "unknown" for one timeout
            # window instead of reporting "away", which would trigger spurious automations.
            return None if now - self._data.started < self._timeout else False
        return now - s.last_seen < self._timeout

    @property
    def extra_state_attributes(self) -> dict:
        s = self._data.seen.get(self._tile_uuid)
        if s is None:
            return {}
        return {"last_seen_s_ago": round(time.monotonic() - s.last_seen), "rssi": s.rssi,
                "address": s.address, "source": s.source, "counter": s.ctr}

    async def async_added_to_hass(self) -> None:
        @callback
        def _seen(tile_uuid: str) -> None:
            if tile_uuid == self._tile_uuid:
                self.async_write_ha_state()

        self.async_on_remove(async_dispatcher_connect(
            self.hass, SIGNAL_SEEN.format(entry_id=self._entry_id), _seen))
        # periodic check for timeout expiry
        self.async_on_remove(async_track_time_interval(
            self.hass, lambda _now: self.async_write_ha_state(), timedelta(seconds=30)))
