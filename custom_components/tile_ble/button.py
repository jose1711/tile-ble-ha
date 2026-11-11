"""Button entity: ring the tracker (requires an active BLE connection)."""
from __future__ import annotations

import logging

from homeassistant.components import bluetooth
from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from . import ring_ble, toa_protocol as p
from .entity import TileBleEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities) -> None:
    data = entry.runtime_data
    async_add_entities(TileRingButton(t, data, hass) for t in data.tiles.values())


class TileRingButton(TileBleEntity, ButtonEntity):
    _attr_name = "Ring"
    _attr_icon = "mdi:bell-ring"

    def __init__(self, tile, data, hass: HomeAssistant) -> None:
        super().__init__(tile)
        self._tile = tile
        self._data = data
        self._hass = hass
        self._attr_unique_id = f"{self._tile_uuid}_ring"

    async def async_press(self) -> None:
        sighting = self._data.seen.get(self._tile_uuid)
        if sighting is None:
            raise HomeAssistantError(
                "Tracker has not been seen - it must be in range of an adapter/proxy before it can be rung")
        ble_device = bluetooth.async_ble_device_from_address(
            self._hass, sighting.address, connectable=True)
        if ble_device is None:
            raise HomeAssistantError(
                f"No active BLE connection to {sighting.address} (the proxy must have active: true)")

        ringer = ring_ble.TileRinger(
            ble_device, self._tile["auth_key"], self._tile["tile_uuid"],
            self._tile.get("expected_tdt_cmd_config"))
        try:
            await ringer.connect()
            await ringer.authenticate()
            await ringer.ring(p.VOLUME_HIGH, 5)
        except Exception as err:  # noqa: BLE001 - we want a readable error in HA, not a traceback
            raise HomeAssistantError(f"Ringing {self._tile['name']} failed: {err}") from err
        finally:
            await ringer.disconnect()
