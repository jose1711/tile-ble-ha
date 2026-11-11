import { createBluetooth } from "node-ble";
import { TileServiceBle, Tile, TileVolume } from "node-tile";
import { readFileSync } from "fs";

const NAME = process.argv[2];
const MAC = process.argv[3]; // optional, for a static MAC
if (!NAME) { console.error("usage: ring_direct.mjs <name> [MAC]"); process.exit(1); }

const tiles = JSON.parse(readFileSync("../../secrets/tiles.json", "utf8"));
const t = tiles.find(x => x.name === NAME && x.auth_key);
if (!t) { console.error("tracker not found:", NAME); process.exit(1); }
console.log("Want:", t.name, t.product, "tile_uuid", t.tile_uuid);

const { bluetooth, destroy } = createBluetooth();

async function tryOne(dev, addr) {
  const svc = new TileServiceBle(dev);
  svc.on("debug", m => console.log(`[${addr}]`, m));
  try {
    await svc.connect();
    const tile = new Tile(t.tile_uuid, t.auth_key, t.name, t.product, null);
    console.log("Authenticating (TDI + handshake)...");
    await svc.authenticate(tile);
    console.log("Device reported tileId:", svc.tileId, "| want:", t.tile_uuid);
    if (svc.tileId !== t.tile_uuid) {
      console.log("tileId MISMATCH -> this is not our tracker.");
      await svc._disconnect().catch(()=>{});
      return false;
    }
    console.log(">>> AUTHENTICATED, correct tracker! Ringing (MED, 5s)...");
    await svc.sendRinger(TileVolume.MED, 5);
    console.log("Command sent, waiting 7s...");
    await new Promise(r => setTimeout(r, 7000));
    await svc._disconnect().catch(()=>{});
    console.log("DONE OK");
    return true;
  } catch (e) {
    console.error("FAIL:", e.message || e);
    try { await svc._disconnect(); } catch {}
    return false;
  }
}

async function main() {
  const adapter = await bluetooth.defaultAdapter();
  if (!await adapter.isDiscovering()) await adapter.startDiscovery().catch(()=>{});

  if (MAC) {
    let dev;
    for (let i = 0; i < 15 && !dev; i++) {
      await new Promise(r => setTimeout(r, 2000));
      try { dev = await adapter.getDevice(MAC); } catch { console.log("waiting for", MAC, "..."); }
    }
    if (!dev) { console.error("device not found in time"); destroy(); process.exit(6); }
    if (await adapter.isDiscovering()) { console.log("stopping discovery before connecting"); await adapter.stopDiscovery().catch(()=>{}); }
    await new Promise(r => setTimeout(r, 1000));
    const ok = await tryOne(dev, MAC);
    destroy();
    process.exit(ok ? 0 : 5);
  } else {
    console.error("Running without a MAC is not supported yet (rotating MAC) - pass the last known MAC from a previous scan.");
    destroy();
    process.exit(1);
  }
}
main().catch(e => { console.error("FATAL", e); destroy(); process.exit(9); });
