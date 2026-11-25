"""Tracker entity: where the tracker was heard last.

- HA's own adapters/proxies count as zone.home; static external scanners pass their zone to
  tile_ble.report_advertisement. No sighting within the presence timeout -> not_home.
- Mobile scanners (a phone) pass coordinates (latitude/longitude or source_tracker). Those are kept as
  the last known location even after the presence timeout ("where did I leave my keys"), survive an HA
  restart and are replaced only by a newer sighting.
"""
from __future__ import annotations

import time
from datetime import timedelta

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.const import ATTR_GPS_ACCURACY, ATTR_LATITUDE, ATTR_LONGITUDE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from . import Location
from .const import CONF_TIMEOUT, DEFAULT_TIMEOUT, SIGNAL_SEEN
from .entity import TileBleEntity

ATTR_LAST_SEEN = "last_seen"
ATTR_SOURCE = "source"


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities) -> None:
    data = entry.runtime_data
    timeout = entry.options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT) * 60
    async_add_entities(TileZoneTracker(t, data, entry.entry_id, timeout) for t in data.trackables.values())


class TileZoneTracker(TileBleEntity, TrackerEntity, RestoreEntity):
    _attr_name = None  # the device name, e.g. device_tracker.keys

    def __init__(self, tile, data, entry_id, timeout) -> None:
        super().__init__(tile)
        self._data, self._entry_id, self._timeout = data, entry_id, timeout
        self._attr_unique_id = f"{self._tile_uuid}_tracker"
        self._attr_source_type = SourceType.BLUETOOTH_LE
        # last known coordinates from before an HA restart: (location, attributes)
        self._restored: tuple[Location, dict] | None = None

    def _set_location(self, loc: Location | None) -> None:
        self._attr_latitude = loc.latitude if loc else None
        self._attr_longitude = loc.longitude if loc else None
        self._attr_location_accuracy = loc.accuracy if loc else 0
        self._attr_source_type = SourceType.GPS if loc else SourceType.BLUETOOTH_LE

    @callback
    def _refresh(self) -> None:
        now = time.monotonic()
        s = self._data.seen.get(self._tile_uuid)
        if s is not None and s.location is not None:
            self._set_location(s.location)
            self._attr_in_zones = None  # HA derives the zone from the coordinates
        elif s is not None:
            self._set_location(None)
            self._attr_in_zones = [s.zone] if now - s.last_seen < self._timeout else []
        elif self._restored is not None:
            self._set_location(self._restored[0])
            self._attr_in_zones = None
        else:
            self._set_location(None)
            # like the presence sensor: unknown (None) for one timeout window after startup
            self._attr_in_zones = None if now - self._data.started < self._timeout else []

        if s is not None:
            self._attr_extra_state_attributes = {
                ATTR_SOURCE: s.source,
                ATTR_LAST_SEEN: dt_util.utc_from_timestamp(s.wall).isoformat(),
            }
        elif self._restored is not None:
            self._attr_extra_state_attributes = self._restored[1]
        else:
            self._attr_extra_state_attributes = {}
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        if (last := await self.async_get_last_state()) is not None:
            a = last.attributes
            if a.get(ATTR_LATITUDE) is not None and a.get(ATTR_LONGITUDE) is not None:
                self._restored = (
                    Location(float(a[ATTR_LATITUDE]), float(a[ATTR_LONGITUDE]), float(a.get(ATTR_GPS_ACCURACY) or 0)),
                    {k: a[k] for k in (ATTR_SOURCE, ATTR_LAST_SEEN) if k in a})

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
