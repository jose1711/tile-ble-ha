import noble from "@abandonware/noble";
noble.on("stateChange", async (state) => {
  console.log("state:", state);
  if (state === "poweredOn") {
    console.log("starting scan...");
    try { await noble.startScanningAsync([], true); console.log("scan started"); }
    catch (e) { console.error("scan start error:", e); }
  }
});
let n = 0;
noble.on("discover", (p) => {
  n++;
  console.log(n, p.address, p.advertisement.localName, (p.advertisement.serviceUuids||[]).join(","));
});
setTimeout(() => { console.log("total discovered:", n); process.exit(0); }, 15000);
