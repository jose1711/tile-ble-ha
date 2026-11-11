#!/usr/bin/env python3
"""Test of the Python ring port (bleak) against a real tracker."""
import asyncio, base64, json, logging, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components", "tile_ble"))
import ring_ble, toa_protocol as p

logging.basicConfig(level=logging.WARNING, format="%(message)s")
logging.getLogger("ring_ble").setLevel(logging.DEBUG)

NAME = sys.argv[1]
MAC = sys.argv[2]

tiles = json.load(open(os.path.join(os.path.dirname(__file__), "..", "secrets", "tiles.json")))
t = next(x for x in tiles if x["name"] == NAME)
print("Tracker:", t["name"], t["tile_uuid"])

async def main():
    await ring_ble.ring_tile(MAC, t["auth_key"], t["tile_uuid"], t.get("expected_tdt_cmd_config"),
                              volume=p.VOLUME_MED, seconds=5)
    print("DONE OK")

asyncio.run(main())
