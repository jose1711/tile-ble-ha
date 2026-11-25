"""tile_ble.report_advertisement: advertisements captured outside HA update the matching tracker."""
import base64
import time
from unittest.mock import MagicMock, patch

import pytest
import voluptuous as vol

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.exceptions import ServiceValidationError  # noqa: E402
from homeassistant.setup import async_setup_component  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from custom_components.tile_ble.const import CONF_TILES, DOMAIN  # noqa: E402
from custom_components.tile_ble.privateid import private_ids  # noqa: E402

KEY = base64.b64encode(bytes(range(16))).decode()
TILES = [{"tile_uuid": "0011223344556677", "auth_key": KEY, "name": "Keys", "product": "MATE"},
         {"tile_uuid": "8899aabbccddeeff", "auth_key": KEY, "name": "Wallet", "product": "SLIM"}]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


async def _setup(hass):
    """Set up the entry; returns the callback HA's bluetooth would call for native advertisements."""
    hass.config.components.add("bluetooth")  # no real adapter in tests
    assert await async_setup_component(hass, "zone", {"zone": [
        {"name": "Chata", "latitude": 48.9, "longitude": 19.6, "radius": 100}]})
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_TILES: TILES})
    entry.add_to_hass(hass)
    register = MagicMock(return_value=lambda: None)
    with patch("custom_components.tile_ble.bluetooth.async_register_callback", register), \
         patch("custom_components.tile_ble.bluetooth.async_discovered_service_info", return_value=[]):
        assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, register.call_args.args[1]


def _sd(tile_uuid, ctr):
    return bytes([2, 0]) + next(p for p, c in private_ids(KEY, tile_uuid).items() if c == ctr)


async def test_report_updates_only_matching_tracker(hass):
    await _setup(hass)
    pid = _sd("8899aabbccddeeff", 42)[2:]
    await hass.services.async_call(DOMAIN, "report_advertisement", {
        "service_data": (bytes([2, 0]) + pid).hex(), "rssi": -61,
        "address": "06:0D:8C:B5:1E:E2", "source": "nucbox"}, blocking=True)
    await hass.async_block_till_done()

    wallet = hass.states.get("binary_sensor.wallet_presence")
    assert wallet.state == "on"
    assert wallet.attributes["source"] == "nucbox"
    assert wallet.attributes["rssi"] == -61
    assert wallet.attributes["counter"] == 42
    assert hass.states.get("binary_sensor.keys_presence").state == "unknown"


async def test_report_unknown_and_invalid(hass):
    await _setup(hass)
    await hass.services.async_call(DOMAIN, "report_advertisement",
                                   {"service_data": "0200" + "41" * 8}, blocking=True)
    assert hass.states.get("binary_sensor.wallet_presence").state == "unknown"
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "report_advertisement",
                                       {"service_data": "zz"}, blocking=True)


async def test_zone_follows_scanner(hass):
    entry, native_adv = await _setup(hass)
    assert hass.states.get("device_tracker.wallet").state == "unknown"

    # external scanner in a static zone
    await hass.services.async_call(DOMAIN, "report_advertisement", {
        "service_data": _sd("8899aabbccddeeff", 7).hex(), "source": "nucbox", "zone": "zone.chata"},
        blocking=True)
    await hass.async_block_till_done()
    st = hass.states.get("device_tracker.wallet")
    assert st.state == "Chata"
    assert st.attributes["in_zones"] == ["zone.chata"]
    assert st.attributes["source"] == "nucbox"
    assert hass.states.get("device_tracker.keys").state == "unknown"

    # heard by HA's own bluetooth -> home
    info = MagicMock(service_data={"0000feed-0000-1000-8000-00805f9b34fb": _sd("8899aabbccddeeff", 8)},
                     rssi=-70, address="AA:BB:CC:DD:EE:FF", source="hci0")
    native_adv(info, None)
    await hass.async_block_till_done()
    assert hass.states.get("device_tracker.wallet").state == "home"

    # nobody heard it within the timeout -> not_home
    entry.runtime_data.seen["8899aabbccddeeff"].last_seen = time.monotonic() - 16 * 60
    entity = hass.data["entity_components"]["device_tracker"].get_entity("device_tracker.wallet")
    entity._refresh()
    assert hass.states.get("device_tracker.wallet").state == "not_home"


async def test_zone_must_be_a_zone(hass):
    await _setup(hass)
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(DOMAIN, "report_advertisement", {
            "service_data": _sd("8899aabbccddeeff", 1).hex(), "zone": "light.kitchen"}, blocking=True)
