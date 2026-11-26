"""Mobile scanners (coordinates / source_tracker) and iBeacons."""
import base64
import time
import uuid as uuidlib
from unittest.mock import MagicMock, patch

import pytest
import voluptuous as vol

from custom_components.tile_ble import ibeacon

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import State  # noqa: E402
from homeassistant.data_entry_flow import FlowResultType  # noqa: E402
from homeassistant.exceptions import ServiceValidationError  # noqa: E402
from homeassistant.helpers import device_registry as dr  # noqa: E402
from homeassistant.setup import async_setup_component  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry, mock_restore_cache  # noqa: E402

from custom_components.tile_ble.const import CONF_IBEACONS, CONF_TILES, DOMAIN  # noqa: E402
from custom_components.tile_ble.privateid import private_ids  # noqa: E402

KEY = base64.b64encode(bytes(range(16))).decode()
TILES = [{"tile_uuid": "8899aabbccddeeff", "auth_key": KEY, "name": "Wallet", "product": "SLIM"}]
BUUID = "2f234454-cf6d-4a0f-adf2-f4911ba9ffa6"
BEACONS = f"Car keys, {BUUID}, 1, 2\nAny bag, {BUUID.upper()}  # any major/minor\n"
CHATA = (48.9, 19.6)
FAR = (48.1486, 17.1077)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def no_bluetooth_manager():
    """No real adapter in tests; also covers entry reloads after an options change."""
    with patch("custom_components.tile_ble.bluetooth.async_discovered_service_info", return_value=[]):
        yield


async def _setup(hass, options=None):
    hass.config.components.add("bluetooth")
    assert await async_setup_component(hass, "zone", {"zone": [
        {"name": "Chata", "latitude": CHATA[0], "longitude": CHATA[1], "radius": 100}]})
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_TILES: TILES}, options=options or {})
    entry.add_to_hass(hass)
    register = MagicMock(return_value=lambda: None)
    with patch("custom_components.tile_ble.bluetooth.async_register_callback", register):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry, [c.args[1] for c in register.call_args_list]


def _sd(ctr=7):
    return (bytes([2, 0]) + next(p for p, c in private_ids(KEY, TILES[0]["tile_uuid"]).items() if c == ctr)).hex()


async def _report(hass, **kw):
    await hass.services.async_call(DOMAIN, "report_advertisement", {"service_data": _sd(), **kw}, blocking=True)
    await hass.async_block_till_done()


# --- pure helpers ---------------------------------------------------------------------------------

def test_parse_config():
    b = ibeacon.parse_config(BEACONS)
    assert [(x["name"], x["major"], x["minor"]) for x in b] == [("Car keys", 1, 2), ("Any bag", None, None)]
    assert b[0]["uuid"] == b[1]["uuid"] == BUUID
    lookup = ibeacon.build_lookup(b)
    assert ibeacon.match(lookup, BUUID, 1, 2) == b[0]["tile_uuid"]
    assert ibeacon.match(lookup, BUUID, 1, 3) == b[1]["tile_uuid"]
    assert ibeacon.match(lookup, str(uuidlib.uuid4()), 1, 2) is None
    for bad in ("x", f"A, nope", f"A, {BUUID}, 70000", f"A, {BUUID}, , 3", f"A, {BUUID}\nB, {BUUID}"):
        with pytest.raises(ibeacon.IBeaconConfigError):
            ibeacon.parse_config(bad)
    assert ibeacon.parse_config("") == []


def test_parse_advertisement():
    md = b"\x02\x15" + uuidlib.UUID(BUUID).bytes + (1).to_bytes(2, "big") + (2).to_bytes(2, "big") + bytes([0xC5])
    assert ibeacon.parse_advertisement(md) == (BUUID, 1, 2, -59)
    assert ibeacon.parse_advertisement(b"\x10\x05" + md[2:]) is None
    assert ibeacon.parse_advertisement(md[:10]) is None


# --- mobile scanner -------------------------------------------------------------------------------

async def test_coordinates_kept_as_last_known_location(hass):
    entry, _ = await _setup(hass)
    await _report(hass, source="pixel", latitude=FAR[0], longitude=FAR[1], gps_accuracy=800)
    st = hass.states.get("device_tracker.wallet")
    assert st.state == "not_home"
    assert (st.attributes["latitude"], st.attributes["longitude"], st.attributes["gps_accuracy"]) == (*FAR, 800)
    assert st.attributes["source_type"] == "gps"
    assert st.attributes["source"] == "pixel"
    assert "last_seen" in st.attributes
    assert hass.states.get("binary_sensor.wallet_presence").state == "on"

    # inside a zone -> that zone
    await _report(hass, latitude=CHATA[0], longitude=CHATA[1])
    assert hass.states.get("device_tracker.wallet").state == "Chata"

    # after the presence timeout the coordinates stay (last known location), presence goes off
    entry.runtime_data.seen[TILES[0]["tile_uuid"]].last_seen = time.monotonic() - 16 * 60
    hass.data["entity_components"]["device_tracker"].get_entity("device_tracker.wallet")._refresh()
    hass.data["entity_components"]["binary_sensor"].get_entity("binary_sensor.wallet_presence").async_write_ha_state()
    assert hass.states.get("device_tracker.wallet").state == "Chata"
    assert hass.states.get("binary_sensor.wallet_presence").state == "off"

    # a static scanner (zone) sets the state; outside that zone the zone center goes on the map
    await _report(hass, zone="zone.home")
    st = hass.states.get("device_tracker.wallet")
    assert st.state == "home" and st.attributes["scanner_zone"] == "zone.home"
    home = hass.states.get("zone.home").attributes
    assert (st.attributes["latitude"], st.attributes["longitude"]) == (home["latitude"], home["longitude"])
    assert st.attributes["gps_accuracy"] == home["radius"]

    # a precise fix inside the zone survives the next static sighting
    near = (home["latitude"] + 0.0003, home["longitude"])  # ~33 m from the center
    await _report(hass, latitude=near[0], longitude=near[1], gps_accuracy=20)
    await _report(hass, zone="zone.home")
    st = hass.states.get("device_tracker.wallet")
    assert st.state == "home" and (st.attributes["latitude"], st.attributes["gps_accuracy"]) == (near[0], 20)

    # static sighting timed out -> not_home, but still on the map
    entry.runtime_data.seen[TILES[0]["tile_uuid"]].last_seen = time.monotonic() - 16 * 60
    hass.data["entity_components"]["device_tracker"].get_entity("device_tracker.wallet")._refresh()
    st = hass.states.get("device_tracker.wallet")
    assert st.state == "not_home" and st.attributes["latitude"] == near[0]


async def test_source_tracker(hass):
    await _setup(hass)
    hass.states.async_set("device_tracker.pixel", "not_home",
                          {"latitude": FAR[0], "longitude": FAR[1], "gps_accuracy": 35})
    await _report(hass, source_tracker="device_tracker.pixel")
    st = hass.states.get("device_tracker.wallet")
    assert (st.attributes["latitude"], st.attributes["gps_accuracy"]) == (FAR[0], 35)

    # tracker without coordinates -> zone fallback
    hass.states.async_set("device_tracker.router", "home", {})
    await _report(hass, source_tracker="device_tracker.router", zone="zone.chata")
    assert hass.states.get("device_tracker.wallet").state == "Chata"

    with pytest.raises(ServiceValidationError):
        await _report(hass, source_tracker="device_tracker.missing")
    with pytest.raises(vol.Invalid):
        await _report(hass, latitude=1.0)  # longitude missing
    with pytest.raises(vol.Invalid):
        await _report(hass, source_tracker="light.kitchen")


async def test_last_location_restored(hass):
    mock_restore_cache(hass, [State("device_tracker.wallet", "not_home", {
        "latitude": FAR[0], "longitude": FAR[1], "gps_accuracy": 500, "source": "pixel",
        "last_seen": "2026-11-20T10:00:00+00:00"})])
    await _setup(hass)
    st = hass.states.get("device_tracker.wallet")
    assert st.state == "not_home"
    assert (st.attributes["latitude"], st.attributes["longitude"]) == FAR
    assert st.attributes["last_seen"] == "2026-11-20T10:00:00+00:00"


async def test_restored_static_sighting(hass):
    """Last heard by a static scanner before a restart: shown at its spot, away after the startup window."""
    mock_restore_cache(hass, [State("device_tracker.wallet", "home", {
        "latitude": CHATA[0], "longitude": CHATA[1], "gps_accuracy": 100, "source": "proxy",
        "scanner_zone": "zone.chata"})])
    entry, _ = await _setup(hass)
    assert hass.states.get("device_tracker.wallet").state == "Chata"
    entry.runtime_data.started = time.monotonic() - 16 * 60
    hass.data["entity_components"]["device_tracker"].get_entity("device_tracker.wallet")._refresh()
    st = hass.states.get("device_tracker.wallet")
    assert st.state == "not_home" and st.attributes["latitude"] == CHATA[0]


async def test_restored_fix_kept_by_zone_sighting(hass):
    """A phone fix from before a restart survives the first static sighting inside that zone."""
    near = (CHATA[0] + 0.0003, CHATA[1])
    mock_restore_cache(hass, [State("device_tracker.wallet", "Chata", {
        "latitude": near[0], "longitude": near[1], "gps_accuracy": 15, "source": "pixel"})])
    await _setup(hass)
    await _report(hass, zone="zone.chata")
    st = hass.states.get("device_tracker.wallet")
    assert st.state == "Chata" and (st.attributes["latitude"], st.attributes["gps_accuracy"]) == (near[0], 15)


# --- iBeacons -------------------------------------------------------------------------------------

async def test_report_ibeacon(hass):
    entry, _ = await _setup(hass, {CONF_IBEACONS: BEACONS})
    assert hass.states.get("device_tracker.car_keys").state == "unknown"
    await hass.services.async_call(DOMAIN, "report_ibeacon", {
        "uuid": BUUID.upper(), "major": 1, "minor": 2, "rssi": -70, "source": "pixel",
        "latitude": FAR[0], "longitude": FAR[1]}, blocking=True)
    await hass.async_block_till_done()
    st = hass.states.get("device_tracker.car_keys")
    assert st.state == "not_home" and st.attributes["latitude"] == FAR[0]
    assert hass.states.get("binary_sensor.car_keys_presence").state == "on"
    assert hass.states.get("device_tracker.any_bag").state == "unknown"  # exact match wins

    await hass.services.async_call(DOMAIN, "report_ibeacon", {
        "uuid": BUUID, "major": 9, "minor": 9, "zone": "zone.chata"}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get("device_tracker.any_bag").state == "Chata"

    # unknown beacon ignored, bad uuid rejected
    await hass.services.async_call(DOMAIN, "report_ibeacon",
                                   {"uuid": str(uuidlib.uuid4()), "major": 1, "minor": 2}, blocking=True)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "report_ibeacon", {"uuid": "x", "major": 1, "minor": 2},
                                       blocking=True)
    dev = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, "ibeacon_2f234454cf6d4a0fadf2f4911ba9ffa6_1_2"), entry.entry_id)
    assert dev.manufacturer == "iBeacon"


async def test_native_ibeacon_and_stale_device_removal(hass):
    entry, callbacks = await _setup(hass, {CONF_IBEACONS: BEACONS})
    assert len(callbacks) == 2
    md = b"\x02\x15" + uuidlib.UUID(BUUID).bytes + (1).to_bytes(2, "big") + (2).to_bytes(2, "big") + b"\xc5"
    callbacks[1](MagicMock(manufacturer_data={0x004C: md}, rssi=-60, address="AA:BB:CC:DD:EE:01",
                           source="hci0"), None)
    await hass.async_block_till_done()
    assert hass.states.get("device_tracker.car_keys").state == "home"

    reg = dr.async_get(hass)
    assert len(dr.async_entries_for_config_entry(reg, entry.entry_id)) == 3
    with patch("custom_components.tile_ble.bluetooth.async_register_callback", MagicMock(return_value=lambda: None)):
        hass.config_entries.async_update_entry(entry, options={CONF_IBEACONS: f"Car keys, {BUUID}, 1, 2"})
        await hass.async_block_till_done()
    names = sorted(d.name for d in dr.async_entries_for_config_entry(reg, entry.entry_id))
    assert names == ["Car keys", "Wallet"]


async def test_options_flow_validates_ibeacons(hass):
    entry, _ = await _setup(hass)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    res = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"timeout": 15, CONF_IBEACONS: "Broken, not-a-uuid"})
    assert res["type"] is FlowResultType.FORM
    assert res["errors"] == {CONF_IBEACONS: "invalid_ibeacons"}
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"timeout": 15, CONF_IBEACONS: BEACONS})
    assert res["type"] is FlowResultType.CREATE_ENTRY


async def test_webhook(hass, hass_client_no_auth):
    """The phone app reports through the entry's webhook (no access token)."""
    assert await async_setup_component(hass, "webhook", {})
    entry, _ = await _setup(hass, {CONF_IBEACONS: BEACONS})
    wid = entry.data["webhook_id"]
    client = await hass_client_no_auth()
    url = f"/api/webhook/{wid}"

    resp = await client.post(url, json={"type": "ping"})
    assert resp.status == 200 and (await resp.json())["tiles"] == 1

    resp = await client.post(url, json={"type": "tile", "service_data": _sd(), "rssi": -60,
                                        "latitude": FAR[0], "longitude": FAR[1], "gps_accuracy": 30})
    assert (await resp.json()) == {"matched": True}
    await hass.async_block_till_done()
    st = hass.states.get("device_tracker.wallet")
    assert st.state == "not_home" and st.attributes["latitude"] == FAR[0]

    resp = await client.post(url, json={"type": "ibeacon", "uuid": BUUID, "major": 1, "minor": 2,
                                        "zone": "zone.chata"})
    assert (await resp.json()) == {"matched": True}
    await hass.async_block_till_done()
    assert hass.states.get("device_tracker.car_keys").state == "Chata"

    for body in ({"type": "tile", "service_data": "zz"}, {"type": "ibeacon", "uuid": BUUID},
                 {"type": "nope"}, ["x"]):
        assert (await client.post(url, json=body)).status == 400
    assert (await client.post(url, data="{")).status == 400

    # regenerating in the options: the old URL stops working, the new one is shown in the form
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    assert wid in flow["description_placeholders"]["webhook_url"]
    with patch("custom_components.tile_ble.bluetooth.async_register_callback",
               MagicMock(return_value=lambda: None)):  # the entry reloads
        await hass.config_entries.options.async_configure(
            flow["flow_id"], {"timeout": 15, CONF_IBEACONS: BEACONS, "regenerate_webhook": True})
        await hass.async_block_till_done()
    new = entry.data["webhook_id"]
    assert new != wid and "regenerate_webhook" not in entry.options
    assert (await client.post(url, json={"type": "ping"})).status == 200  # unknown webhook: HA answers 200
    assert "tiles" not in await (await client.post(url, json={"type": "ping"})).text()
    assert (await (await client.post(f"/api/webhook/{new}", json={"type": "ping"})).json())["ok"]


async def test_webhook_trackables(hass, hass_client_no_auth):
    """The app's tracker list: names, beacon ids and privateIds (windowed once the phase is known)."""
    assert await async_setup_component(hass, "webhook", {})
    entry, _ = await _setup(hass, {CONF_IBEACONS: BEACONS})
    client = await hass_client_no_auth()
    url = f"/api/webhook/{entry.data['webhook_id']}"
    ids = {c: p.hex() for p, c in private_ids(KEY, TILES[0]["tile_uuid"]).items()}

    # phase unknown -> the whole cycle, no expiry
    body = await (await client.post(url, json={"type": "trackables"})).json()
    tile = body["tiles"][0]
    assert tile["name"] == "Wallet" and len(tile["ids"]) == 8641 and tile["valid_until"] is None
    assert "auth_key" not in str(body)
    assert {b["name"] for b in body["beacons"]} == {"Car keys", "Any bag"}

    # heard at ctr 7 -> +-2 days around it, wrapping over the end of the cycle
    await _report(hass)
    tile = (await (await client.post(url, json={"type": "trackables", "window_days": 1})).json())["tiles"][0]
    assert len(tile["ids"]) == 2 * 192 + 1 and tile["valid_until"]
    assert ids[7] in tile["ids"] and ids[8640] in tile["ids"] and ids[199] in tile["ids"]
    assert ids[200] not in tile["ids"] and ids[8440] not in tile["ids"]

    # window_days 0 -> whole table again; invalid -> 400
    tile = (await (await client.post(url, json={"type": "trackables", "window_days": 0})).json())["tiles"][0]
    assert len(tile["ids"]) == 8641
    assert (await client.post(url, json={"type": "trackables", "window_days": -1})).status == 400


async def test_webhook_session_key(hass, hass_client_no_auth):
    """Per-channel key for ringing from the phone; same derivation as ring_ble, auth_key stays in HA."""
    from custom_components.tile_ble import toa_protocol as toa
    assert await async_setup_component(hass, "webhook", {})
    entry, _ = await _setup(hass)
    client = await hass_client_no_auth()
    url = f"/api/webhook/{entry.data['webhook_id']}"
    req = {"type": "session_key", "key": TILES[0]["tile_uuid"], "rand_a": "11" * 14,
           "channel_prefix": 5, "channel_data": "2233", "local_token": "aabbccdd"}
    body = await (await client.post(url, json=req)).json()
    want = toa.generate_hmac(base64.b64decode(KEY), bytes.fromhex("11" * 14), bytes.fromhex("2233"), 5,
                             bytes.fromhex("aabbccdd"))[:16].hex()
    assert body == {"session_key": want, "expected_tdt_cmd_config": None}
    for bad in ({"key": "nope"}, {"rand_a": "11"}, {"channel_data": "zz"}, {"channel_data": "00" * 20}):
        assert (await client.post(url, json={**req, **bad})).status == 400
