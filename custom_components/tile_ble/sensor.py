"""RSSI of the last seen advertisement (useful for estimating the room with multiple proxies)."""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import SIGNAL_STRENGTH_DECIBELS_MILLIWATT, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import CONF_BATTERY_ENABLED, SIGNAL_BATTERY, SIGNAL_SEEN
from .entity import TileBleEntity

BATTERY_ICONS = {"healthy": "mdi:battery", "low": "mdi:battery-alert", "dead": "mdi:battery-off",
                  "unknown": "mdi:battery-unknown"}


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities) -> None:
    data = entry.runtime_data
    entities = [TileRssi(t, data, entry.entry_id) for t in data.tiles.values()]
    if entry.data.get(CONF_BATTERY_ENABLED):
        entities += [TileBattery(t, data, entry.entry_id) for t in data.tiles.values()]
    async_add_entities(entities)


class TileRssi(TileBleEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_name = "RSSI"

    def __init__(self, tile, data, entry_id) -> None:
        super().__init__(tile)
        self._data, self._entry_id = data, entry_id
        self._attr_unique_id = f"{self._tile_uuid}_rssi"

    @property
    def native_value(self):
        s = self._data.seen.get(self._tile_uuid)
        return s.rssi if s else None

    async def async_added_to_hass(self) -> None:
        @callback
        def _seen(tile_uuid: str) -> None:
            if tile_uuid == self._tile_uuid:
                self.async_write_ha_state()

        self.async_on_remove(async_dispatcher_connect(
            self.hass, SIGNAL_SEEN.format(entry_id=self._entry_id), _seen))


class TileBattery(TileBleEntity, SensorEntity):
    """Coarse 3-state battery_status from the Tile cloud (healthy/low/dead) - the only part
    of the integration that needs the cloud, refreshed once per BATTERY_INTERVAL_HOURS."""
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["healthy", "low", "dead", "unknown"]
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Battery"

    def __init__(self, tile, data, entry_id) -> None:
        super().__init__(tile)
        self._data, self._entry_id = data, entry_id
        self._attr_unique_id = f"{self._tile_uuid}_battery"

    @property
    def native_value(self) -> str:
        return self._data.battery.get(self._tile_uuid, "unknown")

    @property
    def icon(self) -> str:
        return BATTERY_ICONS[self.native_value]

    async def async_added_to_hass(self) -> None:
        @callback
        def _updated() -> None:
            self.async_write_ha_state()

        self.async_on_remove(async_dispatcher_connect(
            self.hass, SIGNAL_BATTERY.format(entry_id=self._entry_id), _updated))
