import { createBluetooth } from "node-ble";
const { bluetooth, destroy } = createBluetooth();
try {
  const adapter = await bluetooth.defaultAdapter();
  console.log("adapter ok:", await adapter.getAddress());
  if (!await adapter.isDiscovering()) await adapter.startDiscovery();
  console.log("discovering...");
  await new Promise(r => setTimeout(r, 8000));
  const devs = await adapter.devices();
  console.log("devices seen:", devs.length);
  for (const d of devs.slice(0, 5)) console.log(" -", d);
} catch (e) {
  console.error("ERROR:", e);
} finally {
  destroy();
}
