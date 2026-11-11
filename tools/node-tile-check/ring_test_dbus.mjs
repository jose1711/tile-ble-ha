import { createBluetooth } from "node-ble";
import { TileServiceBle, Tile, TileVolume } from "node-tile";
import { readFileSync } from "fs";
import { execFile } from "child_process";

const NAME = process.argv[2];
if (!NAME) { console.error("usage: ring_test_dbus.mjs <tracker name>"); process.exit(1); }

const tiles = JSON.parse(readFileSync("../../secrets/tiles.json", "utf8"));
const t = tiles.find(x => x.name === NAME && x.auth_key);
if (!t) { console.error("tracker with a key not found:", NAME); process.exit(1); }
console.log("Looking for:", t.name, t.product, "tile_uuid", t.tile_uuid);

const { bluetooth, destroy } = createBluetooth();

function privateIds(authKeyB64, tileIdHex) {
  // delegate the computation to the python tool so the logic is not duplicated; a quick stub is not needed here -
  return null;
}

async function main() {
  const adapter = await bluetooth.defaultAdapter();
  if (!await adapter.isDiscovering()) await adapter.startDiscovery();
  console.log("Scanning FEED service data, looking for the tileId reported by the device...");

  let target = null;
  const deadline = Date.now() + 45000;
  while (Date.now() < deadline && !target) {
    const addrs = await adapter.devices();
    for (const addr of addrs) {
      try {
        const dev = await adapter.getDevice(addr);
        const sd = await dev.getServiceData().catch(() => null);
        if (!sd) continue;
        const feed = sd["0000feed-0000-1000-8000-00805f9b34fb"];
        if (!feed) continue;
        console.log("FEED found at", addr, Buffer.from(feed).toString("hex"));
      } catch {}
    }
    await new Promise(r => setTimeout(r, 2000));
  }

  console.log("Connecting directly to the originally captured MAC address would be unreliable (it rotates).");
  console.log("Instead, trying to connect to EVERY FEED device and verifying the tileId reported by the device.");

  const addrs = await adapter.devices();
  for (const addr of addrs) {
    let dev;
    try { dev = await adapter.getDevice(addr); } catch { continue; }
    const sd = await dev.getServiceData().catch(() => null);
    if (!sd || !sd["0000feed-0000-1000-8000-00805f9b34fb"]) continue;

    console.log("\n>>> Trying to connect to", addr);
    const svc = new TileServiceBle(dev);
    svc.on("debug", m => console.log("[debug]", m));
    try {
      await svc.connect();
      const tileId = await svc.getTileId();
      console.log("tileId from device:", tileId, "want:", t.tile_uuid);
      if (tileId !== t.tile_uuid) {
        console.log("mismatch, disconnecting and trying the next device");
        await svc._disconnect().catch(() => {});
        continue;
      }
      const tile = new Tile(t.tile_uuid, t.auth_key, t.name, t.product, null);
      console.log("Authenticating...");
      await svc.authenticate(tile);
      console.log("Authenticated! Ringing (MED, 5s)...");
      await svc.sendRinger(TileVolume.MED, 5);
      console.log("Ring command sent, waiting 6s...");
      await new Promise(r => setTimeout(r, 6000));
      await svc._disconnect().catch(() => {});
      console.log("DONE OK");
      destroy();
      process.exit(0);
    } catch (e) {
      console.error("FAIL on", addr, e);
      try { await svc._disconnect(); } catch {}
    }
  }
  console.log("Correct tracker not found.");
  destroy();
  process.exit(3);
}

main().catch(e => { console.error("FATAL", e); destroy(); process.exit(9); });
