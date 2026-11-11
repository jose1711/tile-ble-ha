#!/usr/bin/env python3
"""Long scan: logs (time, MAC, tracker, ctr, rssi) to secrets/maclog.jsonl on every MAC/privateId change."""
import asyncio, json, os, sys, time
from bleak import BleakScanner
sys.path.insert(0, os.path.dirname(__file__))
from privateid import private_ids, match
FEED = "0000feed-0000-1000-8000-00805f9b34fb"
d = os.path.join(os.path.dirname(__file__), "..", "secrets")
tiles = [t for t in json.load(open(d + "/tiles.json")) if t.get("auth_key")]
tabs = {t["name"]: private_ids(t["auth_key"], t["tile_uuid"]) for t in tiles}
out = open(d + "/maclog.jsonl", "a", buffering=1)
last = {}
def cb(dev, adv):
    sd = adv.service_data.get(FEED)
    if sd is None: return
    for n, tab in tabs.items():
        c = match(sd, tab)
        if c is not None:
            k = (dev.address, c)
            if last.get(n) != k:
                last[n] = k
                out.write(json.dumps({"t": time.time(), "name": n, "mac": dev.address, "ctr": c,
                                      "sd": sd.hex(), "rssi": adv.rssi}) + "\n")
async def main(sec):
    async with BleakScanner(cb): await asyncio.sleep(sec)
asyncio.run(main(float(sys.argv[1])))
