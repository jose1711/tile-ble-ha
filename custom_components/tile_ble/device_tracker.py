"""Tracker entity: where the tracker was heard last.

- HA's own adapters/proxies count as zone.home; static external scanners pass their zone to
  tile_ble.report_advertisement. No sighting within the presence timeout -> not_home.
  Such a sighting keeps coordinates for the map: the previous ones if they lie inside that zone
  (e.g. the phone's more precise fix at home), otherwise the zone center with its radius as accuracy.
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
from homeassistant.util.location import distance

from . import Location
from .const import CONF_TIMEOUT, DEFAULT_TIMEOUT, SIGNAL_SEEN
from .entity import TileBleEntity

ATTR_LAST_SEEN = "last_seen"
ATTR_SOURCE = "source"
ATTR_SCANNER_ZONE = "scanner_zone"  # set when the last sighting came from a static scanner


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
        self._last_loc: Location | None = None  # coordinates shown last (kept by zone sightings)

    def _set_location(self, loc: Location | None) -> None:
        self._attr_latitude = loc.latitude if loc else None
        self._attr_longitude = loc.longitude if loc else None
        self._attr_location_accuracy = loc.accuracy if loc else 0
        self._attr_source_type = SourceType.GPS if loc else SourceType.BLUETOOTH_LE

    def _zone_location(self, zone_id: str) -> Location | None:
        """Coordinates for a static scanner's sighting: the last ones if inside the zone, else its center."""
        if (z := self.hass.states.get(zone_id)) is None:
            return None
        lat, lon = z.attributes.get(ATTR_LATITUDE), z.attributes.get(ATTR_LONGITUDE)
        if lat is None or lon is None:
            return None
        radius = float(z.attributes.get("radius") or 0)
        prev = self._last_loc
        if prev is not None and (d := distance(prev.latitude, prev.longitude, lat, lon)) is not None \
                and d <= radius:
            return prev
        return Location(float(lat), float(lon), radius)

    @callback
    def _refresh(self) -> None:
        now = time.monotonic()
        s = self._data.seen.get(self._tile_uuid)
        if s is not None and s.location is not None:
            self._set_location(s.location)
            self._attr_in_zones = None  # HA derives the zone from the coordinates
        elif s is not None:
            # a static scanner knows the zone, not the spot; the state follows the zone (not_home after
            # the timeout), the coordinates only keep the tracker on the map
            self._set_location(self._zone_location(s.zone))
            self._attr_in_zones = [s.zone] if now - s.last_seen < self._timeout else []
        elif self._restored is not None:
            self._set_location(self._restored[0])
            # last heard by a static scanner: away once the startup window passes without a sighting
            from_zone = ATTR_SCANNER_ZONE in self._restored[1]
            self._attr_in_zones = [] if from_zone and now - self._data.started >= self._timeout else None
        else:
            self._set_location(None)
            # like the presence sensor: unknown (None) for one timeout window after startup
            self._attr_in_zones = None if now - self._data.started < self._timeout else []

        if self._attr_latitude is not None:
            self._last_loc = Location(self._attr_latitude, self._attr_longitude, self._attr_location_accuracy)
        if s is not None:
            self._attr_extra_state_attributes = {
                ATTR_SOURCE: s.source,
                ATTR_LAST_SEEN: dt_util.utc_from_timestamp(s.wall).isoformat(),
            }
            if s.location is None:
                self._attr_extra_state_attributes[ATTR_SCANNER_ZONE] = s.zone
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
                    {k: a[k] for k in (ATTR_SOURCE, ATTR_LAST_SEEN, ATTR_SCANNER_ZONE) if k in a})

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
