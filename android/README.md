# Tile2HA - Android scanner for `tile_ble`

A small Android app (no Google Play Services, no extra libraries, Android 8+) that turns the phone into
a mobile scanner for the [`tile_ble`](../README.md) Home Assistant integration: when it hears one of
your Tiles or iBeacons it tells HA where the phone was.

## How it saves battery
- **Hardware-filtered scan.** The scan is registered with a `PendingIntent` and filters for the Tile
  `0xFEED` service and the iBeacon UUIDs you configure. The Bluetooth controller does the filtering;
  the app process is not running and is woken only for matching advertisements, batched (60 s) when
  the controller supports it. No foreground service, no notification.
- **Throttling.** Each tracker/beacon is reported at most once per interval (default 5 min).
  Failed reports are retried with the next sighting.
- **Wi-Fi rules**, one per line `SSID = zone`:
  - `HomeWifi = zone.home` (or just `home`) - keep scanning but report that zone instead of a
    location (no location lookup). Without HA Bluetooth proxies at home this keeps the trackers
    `home`; otherwise they keep the last location reported before the phone joined the Wi-Fi.
  - `HomeWifi = none` (or a bare `HomeWifi`) - while connected, the scan is stopped. Use it where
    HA's own Bluetooth adapters/ESPHome proxies cover the place.

  Connecting to Wi-Fi wakes the app; disconnecting does not, so after leaving an excluded network the
  watchdog (every ~15 min) restarts the scan - that is the worst-case delay.
- **Location the phone already has.** Android caches the last fix of every provider (gps, network,
  fused, passive), whoever requested it - the HA Companion app, maps, ... The app takes the best
  cached fix not older than *max age* (default 15 min; accuracy plus ~80 m per minute of age decides
  between an old GPS fix and a newer coarse one). That costs nothing and never turns GPS on.
  Location modes:
  - *Len source_tracker* - no location from the phone; HA takes the coordinates from the phone's HA
    Companion app tracker.
  - *Len uložená poloha* - cached fix only, never a new one; without a fresh enough cached fix the
    report goes out with `source_tracker` only (if set).
  - *Uložená, inak sieťová* - cached fix, otherwise one network fix (Wi-Fi/cell towers, typically
    50-5000 m, Android 11+).

Tiles of other people also wake the app (it has no Tile keys; HA does the matching and ignores
them), so in a busy city the app wakes a bit more often.

## Install
1. Download the APK from the repo **Releases** (tags `app-v*`) or the **Actions** artifact
   `tile2ha-apk`, allow "install unknown apps" for your browser/file manager and install it.
   Obtainium can follow the GitHub releases for updates.
2. In HA: install `tile_ble` 0.3.0+, create a long-lived access token (Profile -> Security).
3. In the app fill in the HA URL, token, location mode, optional `source_tracker`, iBeacon UUIDs and
   excluded SSIDs, then:
   1. **Oprávnenia** - Bluetooth scan + location,
   2. **Poloha na pozadí** - choose "Allow all the time" (needed to receive scan results and read the
      Wi-Fi name in the background),
   3. optionally disable battery optimization (some vendors kill apps otherwise),
   4. **Otestovať spojenie** and **Uložiť a spustiť**. The log at the bottom shows each report.

Location services must be on (Android requires it for BLE scanning and the Wi-Fi name).

## Build
CI (`.github/workflows/android.yml`) builds `assembleRelease` on every change in `android/` and
attaches the APK to releases for `app-v*` tags. Locally: Android SDK + JDK 17, `gradle assembleRelease`
in this directory (Gradle 8.14).

### Signing key (needed for updates)
Without a key every CI build is signed with a different debug key and Android refuses to install the
new version over the old one. Create one key once and store it as repository secrets:

```bash
keytool -genkeypair -v -keystore tile2ha.jks -alias tile2ha -keyalg RSA -keysize 4096 -validity 20000
gh secret set KEYSTORE_BASE64 < <(base64 -w0 tile2ha.jks)
gh secret set KEYSTORE_PASSWORD   # the store password
gh secret set KEY_ALIAS --body tile2ha
gh secret set KEY_PASSWORD        # the key password
```

Keep `tile2ha.jks` safe (not in git).
