import { createBluetooth } from "node-ble";
import { TileServiceBle, UserService, TileVolume } from "node-tile";
import crypto from "crypto";

const NAME = process.argv[2];
const SCAN_SECONDS = Number(process.argv[3] || 45);
const EMAIL = process.env.TILE_EMAIL;
const PASSWORD = process.env.TILE_PASSWORD;
if (!NAME) { console.error("usage: TILE_EMAIL=... TILE_PASSWORD=... node ring.mjs <name> [scan_seconds]"); process.exit(1); }
if (!EMAIL || !PASSWORD) { console.error("TILE_EMAIL/TILE_PASSWORD missing from environment"); process.exit(1); }

const FEED = "0000feed-0000-1000-8000-00805f9b34fb";

// privateId derivation - identical to tools/privateid.py (verified live).
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
    const pid = hmac(seed, ctrBuf).subarray(0, 8).toString("hex");
    table.set(pid, c);
  }
  return table;
}

const { bluetooth, destroy } = createBluetooth();

async function main() {
  const us = new UserService(EMAIL, PASSWORD);
  console.log("Signing in to Tile cloud...");
  await us.login();
  const tiles = await us.getTiles();
  const tile = tiles.find(x => x.name === NAME);
  if (!tile) { console.error("tracker", NAME, "not found. Available:", tiles.map(t => t.name)); destroy(); process.exit(2); }
  console.log("Tile:", tile.name, tile.id);

  const table = buildTable(tile.getAuthKey(), tile.id);
  console.log("privateId table computed (", table.size, "values). Scanning", SCAN_SECONDS, "s...");

  const adapter = await bluetooth.defaultAdapter();
  if (!await adapter.isDiscovering()) await adapter.startDiscovery().catch(() => {});

  let matchedMac = null, matchedCtr = null;
  const deadline = Date.now() + SCAN_SECONDS * 1000;
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
      const pid = buf.subarray(2, 10).toString("hex");
      const ctr = table.get(pid);
      if (ctr !== undefined) { matchedMac = addr; matchedCtr = ctr; break; }
    }
    if (!matchedMac) await new Promise(r => setTimeout(r, 1500));
  }

  if (await adapter.isDiscovering()) await adapter.stopDiscovery().catch(() => {});
  await new Promise(r => setTimeout(r, 1000));

  if (!matchedMac) { console.error("Tracker not found in range during", SCAN_SECONDS, "s."); destroy(); process.exit(3); }
  console.log("Found at", matchedMac, "ctr=", matchedCtr);

  const dev = await adapter.getDevice(matchedMac);
  const svc = new TileServiceBle(dev);
  svc.on("debug", m => console.log("[debug]", m));
  try {
    await svc.connect();
    console.log("Authenticating...");
    await svc.authenticate(tile);
    console.log("device tileId:", svc.tileId, "(on newer firmware this may be the rotating privateId, not the static one - that is ok)");
    console.log(">>> AUTHENTICATED! Ringing (MED, 5s)...");
    await svc.sendRinger(TileVolume.MED, 5);
    console.log("Ring command sent. Waiting 8s...");
    await new Promise(r => setTimeout(r, 8000));
    await svc._disconnect().catch(() => {});
    console.log("DONE OK");
    destroy(); process.exit(0);
  } catch (e) {
    console.error("FAIL:", e);
    try { await svc._disconnect(); } catch {}
    destroy(); process.exit(9);
  }
}
main().catch(e => { console.error("FATAL", e); destroy(); process.exit(10); });
