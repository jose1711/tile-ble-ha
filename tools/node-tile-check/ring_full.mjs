import { createBluetooth } from "node-ble";
import { TileServiceBle, UserService, TileVolume } from "node-tile";

const NAME = process.argv[2];
const MAC = process.argv[3];
const EMAIL = process.env.TILE_EMAIL;
const PASSWORD = process.env.TILE_PASSWORD;
if (!NAME || !MAC) { console.error("usage: TILE_EMAIL=... TILE_PASSWORD=... node ring_full.mjs <name> <MAC>"); process.exit(1); }
if (!EMAIL || !PASSWORD) { console.error("TILE_EMAIL/TILE_PASSWORD missing from environment"); process.exit(1); }

const { bluetooth, destroy } = createBluetooth();

async function main() {
  const us = new UserService(EMAIL, PASSWORD);
  console.log("Signing in to Tile cloud (only to download expected_tdt_cmd_config)...");
  await us.login();
  const tiles = await us.getTiles();
  const tile = tiles.find(x => x.name === NAME);
  if (!tile) { console.error("tracker", NAME, "not found in account. Available:", tiles.map(t=>t.name)); destroy(); process.exit(2); }
  console.log("Found tile:", tile.name, tile.id, "expectedTdtCmdConfig length:", (tile.getExpectedTdtCmdConfig()||"").length);

  const adapter = await bluetooth.defaultAdapter();
  if (!await adapter.isDiscovering()) {
    console.log("Starting a short scan so BlueZ knows", MAC, "...");
    await adapter.startDiscovery().catch(()=>{});
  }

  let dev;
  for (let i = 0; i < 12 && !dev; i++) {
    try { dev = await adapter.getDevice(MAC); }
    catch { console.log("waiting for", MAC, "..."); await new Promise(r=>setTimeout(r,1500)); }
  }
  if (await adapter.isDiscovering()) await adapter.stopDiscovery().catch(()=>{});
  await new Promise(r => setTimeout(r, 1000)); // let BlueZ finish stopping discovery before connect
  if (!dev) { console.error("MAC", MAC, "not found even after scanning (the tracker may be out of range)"); destroy(); process.exit(3); }

  const svc = new TileServiceBle(dev);
  svc.on("debug", m => console.log("[debug]", m));
  try {
    await svc.connect();
    console.log("Authenticating...");
    await svc.authenticate(tile);
    console.log("device tileId:", svc.tileId, "| expected (static):", tile.id);
    if (svc.tileId !== tile.id) {
      // On newer firmware (privateId-capable) TDI TILE_ID returns the current rotating
      // privateId value, not the static tileId - this is normal, not an error.
      // Identity was already verified by a fresh scan_verify.py before you got this MAC.
      console.log("(normal for newer firmware with privateId - TDI returns the rotating ID, not the static one; continuing)");
    }
    console.log(">>> AUTHENTICATED! Ringing (MED, 5s)...");
    await svc.sendRinger(TileVolume.MED, 5);
    console.log("Ring command sent. Waiting 8s...");
    await new Promise(r => setTimeout(r, 8000));
    await svc._disconnect().catch(()=>{});
    console.log("DONE OK");
    destroy(); process.exit(0);
  } catch (e) {
    console.error("FAIL:", e);
    try { await svc._disconnect(); } catch {}
    destroy(); process.exit(9);
  }
}
main().catch(e => { console.error("FATAL", e); destroy(); process.exit(10); });
