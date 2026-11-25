#!/usr/bin/env python3
"""Scan BLE for Tile trackers (service 0xFEED) and feed them to the tile_ble HA integration
(https://github.com/jose1711/tile-ble-ha) via its tile_ble.report_advertisement service.
The integration matches the privateId and updates the specific tracker's entities.

Env: HA_URL (e.g. http://homeassistant.local:8123), HA_TOKEN (long-lived access token),
     TILE_ZONE (static HA zone this scanner is in, e.g. zone.chata; default zone.home),
     TILE_SOURCE (default: hostname), TILE_REPORT_INTERVAL (seconds per advertisement, default 30).
"""
import asyncio, json, os, socket, sys, time, urllib.request

from bleak import BleakScanner

TILE_UUID = "0000feed-0000-1000-8000-00805f9b34fb"
HA_URL = os.environ["HA_URL"].rstrip("/")
HA_TOKEN = os.environ["HA_TOKEN"]
SOURCE = os.environ.get("TILE_SOURCE") or socket.gethostname()
ZONE = os.environ.get("TILE_ZONE") or "zone.home"
INTERVAL = int(os.environ.get("TILE_REPORT_INTERVAL", "30"))

last_sent: dict[tuple[str, str], float] = {}


def _post(data):
    req = urllib.request.Request(
        f"{HA_URL}/api/services/tile_ble/report_advertisement",
        data=json.dumps(data).encode(),
        headers={"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            print(f"reported ({r.status}): {data}", flush=True)
    except Exception as e:
        print(f"report failed: {e}", file=sys.stderr, flush=True)


def on_adv(device, adv):
    sd = adv.service_data.get(TILE_UUID)
    if not sd:
        return
    key, now = (device.address, sd.hex()), time.monotonic()
    if now - last_sent.get(key, -INTERVAL) < INTERVAL:
        return
    last_sent[key] = now
    data = {"service_data": sd.hex(), "rssi": adv.rssi, "address": device.address, "source": SOURCE, "zone": ZONE}
    # urllib blocks; keep the BLE callback (event loop) free
    asyncio.get_running_loop().run_in_executor(None, _post, data)


async def main():
    async with BleakScanner(on_adv):
        print(f"scanning for Tile, reporting to {HA_URL} as {SOURCE!r} in {ZONE}", flush=True)
        await asyncio.Event().wait()


asyncio.run(main())
