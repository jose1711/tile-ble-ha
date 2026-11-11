# node-tile-check

Checks that the [node-tile](https://www.npmjs.com/package/node-tile) library (Lesley De Keyser) can
actually ring our trackers. Uses `node-ble` (BlueZ over D-Bus) - works with a running `bluetoothd`
without conflict (unlike `@abandonware/noble`, which needs direct HCI access and clashed with BlueZ
on the tested adapter).

## Setup
```sh
npm install
```
(node_modules is not in git - `@abandonware/noble`/`bluetooth-hci-socket` have native build scripts,
so you may need `npm install-scripts approve <package>` if npm blocks install scripts.)

## Usage
`ring.mjs` is the finished tool: it signs in to the Tile cloud itself (via `node-tile`'s `UserService`,
to download `auth_key` and `expected_tdt_cmd_config`), computes the `privateId` table, scans the
surroundings briefly, finds the tracker's current MAC and rings it.

```sh
TILE_EMAIL=... TILE_PASSWORD=... node ring.mjs "<tracker name>" [scan_seconds=45]
```

The older `ring_test.mjs` / `ring_test_dbus.mjs` / `ring_direct.mjs` / `ring_full.mjs` / `diag.mjs` /
`ble_diag.mjs` were intermediate steps while looking for a working path (noble vs node-ble) -
`ring.mjs` supersedes them.

## Note on TDI TILE_ID
On older trackers (without privateId, e.g. ROYAL_ST1 fw 48.x) the TDI `TILE_ID` command returns the
real static ID matching the cloud `tile_uuid`. On newer ones (privateId-capable, e.g. ATM24
fw 59.x) it returns the **current rotating `privateId`**, not the static ID - this is not a bug, just
a firmware property. We verify a tracker's identity by matching the `privateId` during scanning, not
via this TDI readback.
