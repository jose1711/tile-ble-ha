#!/usr/bin/env python3
"""Tries to read the battery level (0x2A19) and device info from the given tracker, without Tile auth."""
import asyncio, sys
from bleak import BleakClient

MAC = sys.argv[1]
BATTERY = "00002a19-0000-1000-8000-00805f9b34fb"

async def main():
    async with BleakClient(MAC, timeout=35) as c:
        print("Connected. Services and characteristics:")
        for svc in c.services:
            print(" service", svc.uuid)
            for ch in svc.characteristics:
                print("   char", ch.uuid, ch.properties)
        try:
            val = await c.read_gatt_char(BATTERY)
            print("BATTERY LEVEL:", val[0], "%", "raw=", val.hex())
        except Exception as e:
            print("Battery char unreadable/not found:", e)

asyncio.run(main())
