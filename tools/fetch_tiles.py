#!/usr/bin/env python3
"""Downloads tile_uuid + auth_key + expected_tdt_cmd_config from the Tile cloud into secrets/tiles.json.
The password is requested via getpass; nothing is printed except tracker names and yes/no for expected_tdt_cmd_config."""
import asyncio, getpass, json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components", "tile_ble"))
from aiohttp import ClientSession
import tile_cloud

OUT = os.path.join(os.path.dirname(__file__), "..", "secrets", "tiles.json")

async def main():
    email = os.environ.get("TILE_EMAIL") or input("Tile e-mail: ")
    pw = os.environ.get("TILE_PASSWORD") or getpass.getpass("Tile password: ")
    async with ClientSession() as s:
        _, tiles = await tile_cloud.fetch_tiles(s, email, pw)
        for t in tiles:
            print(t["name"], "| tdt_cfg:", "yes" if t.get("expected_tdt_cmd_config") else "no")
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        fd = os.open(OUT, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(tiles, f, indent=2)
        print("saved:", os.path.normpath(OUT))

asyncio.run(main())
