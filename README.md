# tile-ble-ha

Local Tile tracker presence in Home Assistant over BLE, **without polling the Tile cloud**.
A tracker is recognized by its rotating `privateId` (an HMAC derived from `authKey`), so it does not
matter that some models rotate their MAC address.

## How it works
- Once (in the config flow) you sign in to your Tile account and the integration downloads each
  tracker's `tile_uuid` + `auth_key`. The password is not stored.
- The integration computes a table of 8641 `privateId` values per tracker and passively listens for
  advertisements carrying the `0xFEED` service (local adapter and ESPHome Bluetooth proxies alike).
- Entities per tracker: a presence `binary_sensor` (timeout in Options, default 15 min), a diagnostic
  RSSI sensor, a `button` to ring the tracker (needs an active BLE connection, not just passive
  scanning) and an optional battery `sensor`.

## Battery (optional, the only cloud part)
Tile does not expose the battery over BLE (no standard Battery Service, no Tile TOA command) - only a
coarse 3-state `battery_status` from the cloud (healthy/low/dead, not a percentage; mapping in
`tile_cloud.py` follows `Tile.BatteryStatus` from the decompiled app). It is enabled in the config
flow, requires storing the Tile credentials in the config entry and queries the cloud once every
`BATTERY_INTERVAL_HOURS` (24 h, `const.py`). Presence tracking and ringing stay fully local even
when this is enabled.

## Ringing (button) and ESPHome
`ring_ble.py`/`toa_protocol.py` are a `bleak` port of the TOA/MEP protocol (from
[node-tile](https://www.npmjs.com/package/node-tile) and the decompiled app), verified live on 5
trackers (ROYAL_ST1, ROYAL_ATM1, ATM24_MATE1) via `tools/ring_py_test.py`. The button uses the last
MAC from a sighting by the passive listener and connects through
`bluetooth.async_ble_device_from_address(..., connectable=True)` - **no custom ESPHome code is
needed**: HA can open the GATT connection through a remote ESP32 as long as its `bluetooth_proxy`
has `active: true` (a passive-only proxy cannot ring, it only supports presence tracking).

### ESPHome proxy setup

No custom ESPHome firmware or external component - the standard
[`bluetooth_proxy`](https://esphome.io/components/bluetooth_proxy.html) component in the ESP32 YAML
config is enough:

```yaml
esp32:
  board: ...  # your board
  framework:
    type: esp-idf  # bluetooth_proxy requires esp-idf, not arduino

bluetooth_proxy:
  active: true   # without this the proxy only scans passively - presence works, ringing does not

esp32_ble_tracker:
  scan_parameters:
    active: true
```

After flashing, the proxy shows up in HA as a regular Bluetooth device (Settings → Devices →
ESPHome) and HA integrations (including `tile_ble`) start using it automatically as an additional
Bluetooth adapter - nothing needs to be configured in `tile_ble` itself. With several proxies in the
house HA picks the strongest signal and can estimate which proxy (room) a tracker is near (the
`source` attribute on the presence `binary_sensor`).

**Unverified**: this is the standard HA/ESPHome pattern (documented above), but I had no chance to
test `tile_ble` running through a remote proxy (rather than a local adapter) - I have no HA instance
or ESPHome device running here. Please verify it yourself and report back if something does not fit.

Protocol details: `research/protocol-notes.md`.

## Installation
Copy `custom_components/tile_ble` into your HA config (or add the repo to HACS as a custom
repository) and restart HA.

## Status
Skeleton 0.1.0. Import against HA 2026.11.1 works, the unit tests in `tests/` pass (privateId and
TOA/MEP frames verified against live captures). The ring protocol is verified outside HA
(`ring_py_test.py` + `bleak`); the `button` entity has not yet actually been exercised inside HA
itself. TODO: reauth/key refresh, tests with `pytest-homeassistant-custom-component`,
device_tracker/Bermuda integration, TRM (RSSI from the tracker).

## Tools (`tools/`)
- `fetch_tiles.py` - downloads `tile_uuid`/`auth_key`/`expected_tdt_cmd_config` into `secrets/tiles.json` (kept out of git)
- `scan_verify.py`, `mac_log.py` - verify the privateId algorithm with a live BLE scan, log MACs
- `privateid.py` - reference privateId derivation (Python, identical to `custom_components/tile_ble/privateid.py`)
- `ring_py_test.py` - test of the Python ring port (`bleak`) against one tracker
- `node-tile-check/` - Node.js ring check using the `node-tile` library (predecessor of the Python port)
