import noble from "@abandonware/noble";
import { TileServiceNoble, Tile, TileVolume } from "node-tile";
import { readFileSync } from "fs";

const NAME = process.argv[2];
if (!NAME) { console.error("usage: ring_test.mjs <tracker name from tiles.json>"); process.exit(1); }

const tiles = JSON.parse(readFileSync("../../secrets/tiles.json", "utf8"));
const t = tiles.find(x => x.name === NAME && x.auth_key);
if (!t) { console.error("tracker with a key not found:", NAME); process.exit(1); }
console.log("Looking for:", t.name, t.product, "tile_uuid", t.tile_uuid);

let found = false;
const timeoutMs = 60000;
const timer = setTimeout(() => { console.error("Timeout - tracker not found in", timeoutMs, "ms"); process.exit(2); }, timeoutMs);

noble.on("stateChange", async (state) => {
  console.log("noble state:", state);
  if (state === "poweredOn") await noble.startScanningAsync([], false);
});

noble.on("discover", async (peripheral) => {
  const svcs = (peripheral.advertisement.serviceUuids || []).map(s => s.toLowerCase());
  if (!svcs.includes("feed")) return;
  const sd = (peripheral.advertisement.serviceData || []).find(s => s.uuid.toLowerCase() === "feed");
  if (!sd) return;
  // verify privateId via our existing table (calling python would be awkward, so we only try the MAC heuristic as a filter below)
  if (found) return;
  console.log("FEED peripheral:", peripheral.address, "sd=", sd.data.toString("hex"), "rssi", peripheral.rssi);
  // Try to connect and authenticate - if the device's tileId matches, it is our tracker.
  await noble.stopScanningAsync();
  found = true;
  clearTimeout(timer);

  const svc = new TileServiceNoble(peripheral);
  svc.on("debug", m => console.log("[debug]", m));
  svc.on("rssi", r => console.log("[rssi]", r));

  try {
    await svc.connect();
    const tileId = await svc.getTileId();
    console.log("Device tileId:", tileId, "expected:", t.tile_uuid);
    if (tileId !== t.tile_uuid) {
      console.error("tileId MISMATCH - this is not the requested tracker, disconnecting.");
      await svc._disconnect();
      process.exit(3);
    }
    const tile = new Tile(t.tile_uuid, t.auth_key, t.name, t.product, null);
    console.log("Authenticating...");
    await svc.authenticate(tile);
    console.log("Authenticated! Ringing (MED, 5s)...");
    await svc.sendRinger(TileVolume.MED, 5);
    console.log("Ring command sent.");
    await new Promise(r => setTimeout(r, 6000));
    await svc._disconnect();
    console.log("OK, done.");
    process.exit(0);
  } catch (e) {
    console.error("FAIL:", e);
    try { await svc._disconnect(); } catch {}
    process.exit(4);
  }
});
