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

## Scanners outside HA (`tile_ble.report_advertisement`)
A machine that is not an HA Bluetooth adapter/proxy (e.g. a Linux box running `bleak`) can feed
advertisements it captured through the `tile_ble.report_advertisement` service:

```bash
curl -X POST -H "Authorization: Bearer $HA_TOKEN" -H "Content-Type: application/json" \
  -d '{"service_data": "02005034270c2859af20", "rssi": -60, "address": "06:0D:8C:B5:1E:E2", "source": "nucbox", "zone": "zone.chata"}' \
  "$HA_URL/api/services/tile_ble/report_advertisement"
```

`service_data` is the hex of the `0xFEED` service data. The tracker is identified by its privateId
just like for HA's own adapters, so only the matching tracker's entities are updated (`source` shows
up on the presence sensor). Ringing still needs HA's own connectable adapter/proxy in range.

Optional `zone` (e.g. `"zone": "zone.chata"`) is the static zone where that scanner is located. Each
tracker has a `device_tracker` entity that moves to the zone of the scanner that heard it last
(HA's own adapters/proxies count as `zone.home`) and becomes `not_home` when nobody has heard it within
the presence timeout. It can be assigned to a `person`.

### Remote scanner on any Linux box (`tools/remote_scanner/`)
Any Linux machine with a Bluetooth adapter (BlueZ) that can reach HA's HTTP API (LAN, VPN such as
Netbird/Tailscale/WireGuard, or Nabu Casa) can act as a scanner for a static zone - e.g. a mini PC at
a cottage or in an office. `tile2ha.py` scans for `0xFEED` advertisements with `bleak` and
calls `tile_ble.report_advertisement` for each one (the same advertisement at most once per
`TILE_REPORT_INTERVAL` seconds). It does not need the Tile keys: matching the privateId happens in HA.

1. In HA create a [zone](https://www.home-assistant.io/integrations/zone/) for the place where the
   box is located and a long-lived access token (Profile -> Security -> Long-lived access tokens).
2. On the Linux box (as a regular user; BlueZ allows scanning over D-Bus without root):

   ```bash
   mkdir -p ~/.local/share/tile2ha ~/.config/systemd/user
   python3 -m venv ~/.local/share/tile2ha/venv
   ~/.local/share/tile2ha/venv/bin/pip install bleak
   cp tools/remote_scanner/tile2ha.py ~/.local/share/tile2ha/
   cp tools/remote_scanner/env.example ~/.local/share/tile2ha/env
   chmod 600 ~/.local/share/tile2ha/env    # then fill in HA_URL, HA_TOKEN, TILE_ZONE
   cp tools/remote_scanner/tile2ha.service ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable --now tile2ha
   sudo loginctl enable-linger "$USER"      # keep running without an active login / after reboot
   ```

3. Check it: `journalctl --user -u tile2ha -f` should show `reported (200): ...` lines, and the
   tracker's `device_tracker` switches to the box's zone. HTTP `400` means HA runs a `tile_ble`
   version without the service; `401` a wrong token.

Several boxes in different zones can run at once; the tracker follows the one that heard it last.
Advertisements of trackers that are not in your Tile account are ignored by HA.

### Mobile scanner: Android app (`android/`)
A phone can be a scanner too: it reports where it was when it heard the tracker. Pass coordinates
(`latitude`, `longitude`, optional `gps_accuracy`) or `source_tracker` (an existing `device_tracker`/`person`,
typically the HA Companion app of that phone - HA copies its current coordinates) instead of `zone`:

```json
{"service_data": "02005034270c2859af20", "rssi": -70, "source": "pixel",
 "latitude": 48.1486, "longitude": 17.1077, "gps_accuracy": 800, "source_tracker": "device_tracker.pixel_8"}
```

Explicit coordinates win over `source_tracker`, which wins over `zone` (used only if the tracker has no
coordinates). A sighting with coordinates sets the `device_tracker` to those coordinates (HA derives
the zone / `not_home`, `source_type: gps`), and they are **kept as the last known location** after the
presence timeout and across HA restarts ("where did I leave my keys"), until a newer sighting replaces
them. The `last_seen` attribute says when that was. Presence (`binary_sensor`) still turns off after
the timeout.

The Android app **Tile2HA** in [`android/`](android/README.md) does this with little battery use: the
scan is filtered in the Bluetooth controller and the app is woken only when a Tile/iBeacon is heard.
Per Wi-Fi network it can report a fixed zone instead of a location (`HomeWifi = zone.home`) or skip
scanning entirely (`HomeWifi = none`, e.g. at home where HA's own adapters/proxies cover it), and it uses network location (Wi-Fi/cell, no GPS, no Google Play Services) or `source_tracker`.

## iBeacons
Besides Tiles, the integration can track iBeacons with the same entities (presence, RSSI,
`device_tracker` incl. mobile scanners). List them in the integration **Options**, one per line:

```
Car keys, 2f234454-cf6d-4a0f-adf2-f4911ba9ffa6, 1, 2
Any bag, 2f234454-cf6d-4a0f-adf2-f4911ba9ffa6   # any major/minor
```

The most specific line wins. HA's own adapters/proxies are listened to directly (`zone.home`); other
scanners call `tile_ble.report_ibeacon` with `uuid`, `major`, `minor` plus the same scanner fields as
`report_advertisement` (`rssi`, `address`, `source`, `zone`, `latitude`/`longitude`/`gps_accuracy`,
`source_tracker`). Removing a line removes its device. If the built-in **iBeacon Tracker** integration
also sees the beacon, it has its own separate entity (home/away + distance, HA adapters only); disable
one of them, or assign both to a `person`.

## Installation
Copy `custom_components/tile_ble` into your HA config (or add the repo to HACS as a custom
repository) and restart HA.

## Status
Skeleton 0.1.0. Import against HA 2026.11.1 works, the unit tests in `tests/` pass (privateId and
TOA/MEP frames verified against live captures). The ring protocol is verified outside HA
(`ring_py_test.py` + `bleak`); the `button` entity has not yet actually been exercised inside HA
itself. TODO: reauth/key refresh, tests with `pytest-homeassistant-custom-component`,
Bermuda integration, TRM (RSSI from the tracker).

## Tools (`tools/`)
- `fetch_tiles.py` - downloads `tile_uuid`/`auth_key`/`expected_tdt_cmd_config` into `secrets/tiles.json` (kept out of git)
- `scan_verify.py`, `mac_log.py` - verify the privateId algorithm with a live BLE scan, log MACs
- `privateid.py` - reference privateId derivation (Python, identical to `custom_components/tile_ble/privateid.py`)
- `ring_py_test.py` - test of the Python ring port (`bleak`) against one tracker
- `node-tile-check/` - Node.js ring check using the `node-tile` library (predecessor of the Python port)
