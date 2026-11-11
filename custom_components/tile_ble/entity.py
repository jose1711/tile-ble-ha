from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN


class TileBleEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, tile: dict) -> None:
        self._tile_uuid = tile["tile_uuid"]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._tile_uuid)}, name=tile["name"],
            manufacturer="Tile", model=tile.get("product"))
