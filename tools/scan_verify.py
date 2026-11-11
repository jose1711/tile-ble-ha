#!/usr/bin/env python3
"""Scans BLE, looks for service data 0xFEED and matches privateId against the table from secrets/tiles.json."""
import asyncio, json, os, sys
from bleak import BleakScanner
sys.path.insert(0, os.path.dirname(__file__))
from privateid import private_ids, match

FEED = "0000feed-0000-1000-8000-00805f9b34fb"
base = os.path.join(os.path.dirname(__file__), "..", "secrets", "tiles.json")
tiles = [t for t in json.load(open(base)) if t.get("auth_key")]
tables = {t["name"]: private_ids(t["auth_key"], t["tile_uuid"]) for t in tiles}
legacy = {bytes.fromhex(t["tile_uuid"]): t["name"] for t in tiles}
seen = {}

def cb(dev, adv):
    sd = adv.service_data.get(FEED)
    if sd is None:
        return
    r = "?"
    for name, tab in tables.items():
        c = match(sd, tab)
        if c is not None:
            r = f"v2 {name} ctr={c}"
    if sd[2:10] in legacy:
        r = f"v0 {legacy[sd[2:10]]}"
    key = (dev.address, sd.hex())
    if key not in seen:
        seen[key] = 1
        print(f"{dev.address} rssi={adv.rssi} sd={sd.hex()} -> {r}", flush=True)

async def main(sec):
    async with BleakScanner(cb):
        await asyncio.sleep(sec)
asyncio.run(main(float(sys.argv[1]) if len(sys.argv) > 1 else 30))
