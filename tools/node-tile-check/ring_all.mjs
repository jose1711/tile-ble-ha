import { createBluetooth } from "node-ble";
import { TileServiceBle, UserService, TileVolume } from "node-tile";
import crypto from "crypto";

const SCAN_SECONDS = Number(process.argv[2] || 45);
const EMAIL = process.env.TILE_EMAIL;
const PASSWORD = process.env.TILE_PASSWORD;
if (!EMAIL || !PASSWORD) { console.error("TILE_EMAIL/TILE_PASSWORD missing from environment"); process.exit(1); }

const FEED = "0000feed-0000-1000-8000-00805f9b34fb";

function hmac(key, msg) {
  const padded = Buffer.alloc(32);
  msg.copy(padded);
  return crypto.createHmac("sha256", key).update(padded).digest();
}
function buildTable(authKeyB64, tileIdHex, count = 8641) {
  const authKey = Buffer.from(authKeyB64, "base64");
  const tileId = Buffer.from(tileIdHex, "hex");
  const seed = hmac(authKey, Buffer.concat([tileId, Buffer.from("identity")])).subarray(0, 16);
  const table = new Map();
  const ctrBuf = Buffer.alloc(4);
  for (let c = 0; c < count; c++) {
    ctrBuf.writeUInt32LE(c, 0);
    table.set(hmac(seed, ctrBuf).subarray(0, 8).toString("hex"), c);
  }
  return table;
}

const { bluetooth, destroy } = createBluetooth();

async function findAndRing(adapter, tile, table, scanSeconds) {
  if (!await adapter.isDiscovering()) await adapter.startDiscovery().catch(() => {});
  let matchedMac = null;
  const deadline = Date.now() + scanSeconds * 1000;
  while (Date.now() < deadline && !matchedMac) {
    const addrs = await adapter.devices().catch(() => []);
    for (const addr of addrs) {
      let dev;
      try { dev = await adapter.getDevice(addr); } catch { continue; }
      const sd = await dev.getServiceData().catch(() => null);
      const feed = sd && sd[FEED];
      if (!feed) continue;
      const buf = Buffer.from(feed);
      if (buf.length < 10 || buf[0] !== 2) continue;
      if (table.has(buf.subarray(2, 10).toString("hex"))) { matchedMac = addr; break; }
    }
    if (!matchedMac) await new Promise(r => setTimeout(r, 1500));
  }
  if (await adapter.isDiscovering()) await adapter.stopDiscovery().catch(() => {});
  await new Promise(r => setTimeout(r, 800));
  if (!matchedMac) { console.log("  not found in range, skipping"); return false; }
  console.log("  found at", matchedMac);

  const dev = await adapter.getDevice(matchedMac);
  const svc = new TileServiceBle(dev);
  try {
    await svc.connect();
    await svc.authenticate(tile);
    console.log("  authenticated, ringing...");
    await svc.sendRinger(TileVolume.MED, 5);
    await new Promise(r => setTimeout(r, 6000));
    await svc._disconnect().catch(() => {});
    console.log("  OK");
    return true;
  } catch (e) {
    console.log("  FAIL:", e.message || e);
    try { await svc._disconnect(); } catch {}
    return false;
  }
}

async function main() {
  const us = new UserService(EMAIL, PASSWORD);
  console.log("Signing in to Tile cloud...");
  await us.login();
  const tiles = await us.getTiles();
  console.log("Trackers in account:", tiles.map(t => t.name).join(", "));

  const adapter = await bluetooth.defaultAdapter();
  const results = [];
  for (const tile of tiles) {
    console.log(`\n=== ${tile.name} ===`);
    const table = buildTable(tile.getAuthKey(), tile.id);
    const ok = await findAndRing(adapter, tile, table, SCAN_SECONDS);
    results.push([tile.name, ok]);
  }

  console.log("\n--- Summary ---");
  for (const [name, ok] of results) console.log(ok ? "✓" : "✗", name);
  destroy();
  process.exit(results.every(([, ok]) => ok) ? 0 : 1);
}
main().catch(e => { console.error("FATAL", e); destroy(); process.exit(10); });
