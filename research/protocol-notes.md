# Tile BLE - protocol notes

Source: Kumar, Raymaker, Specter, "Security and Privacy Analysis of Tile's Location Tracking Protocol"
(arXiv 2510.00350, USENIX Sec 2026), Tile Mate 2022, app v2.125.0. Below is only what the paper
states; gaps are marked as unknown and need to be verified in the APK (jadx) / on a device.

## Rotating identifier (privateId)
- Advertised service: FEED (activated tile), FEEC (before activation). The paper does not give the full 128-bit UUID.
- `seed      = HMAC-SHA256(authKey, tileId || "identity")`   (paper: "padded to 32 B" - padding UNKNOWN)
- `privateId = HMAC-SHA256(seed, ctr)[0:8]`  (8 B; encoding of ctr UNKNOWN)
- ctr starts at 0, +1 every 15 min, 8640 values → 90-day cycle. No time/nonce/AES.
- A scanner holding `authKey` can precompute all 8640 values and compare them with the service data.
- payload_service_data in the finders' upload = privateId (the paper gives no offsets within the advertisement).

## Keys
- tileId: 8 B, derived from the static MAC (derivation UNKNOWN); read via the TDI char
  `9d410007-35d6-f4dd-ba60-e7bd8dc491c0`.
- authKey: 16 B, permanent shared secret; the server returns it to the owner after activation.
  The cloud knows it → obtainable from the Tile API/app (the app stores it locally - look in /data/data on a rooted phone).
- interimAuthKey: 16 B, factory only, not needed.

## MAC
- Paper: Tile Mate 2022 uses a STATIC MAC. A user observes rotation → probably a different
  model/firmware, or a confusion of privateId rotation (in the service data) with MAC rotation. Verify by scanning.

## Other UUIDs (FEED)
- MEP_TOA_CMD `9d410018-…`, MEP_TOA_RSP `9d410019-…`, ring `9d410002-…`
  (suffix `-35d6-f4dd-ba60-e7bd8dc491c0`)

## Other sources
- node-tile (lesleyxyz/node-tile) - RE of ringing/communication, useful for filling in the layout
- pytile (bachya) - cloud API
- jadx: https://github.com/skylot/jadx

## VERIFIED in the APK (com.thetileapp.tile 2.148.0, jadx; p334pd/a.java, p292nb/b.java, We/d.java)
Applies INSTEAD of the paper wherever they differ:
- seed      = HMAC-SHA256(authKey, pad32(tileId[8] || "identity"))[:16]   (truncated to 16 B!)
- privateId = HMAC-SHA256(seed, pad32(uint32 LE ctr))[:8], ctr = 0..8640 (8641 values; the app precomputes them into a DB)
- HMAC keys: authKey = base64decode(auth_key), tileId = hex(tile_uuid); the message is zero-padded to 32 B.
- Advertisement: UUID 0000FEED-0000-1000-8000-00805F9B34FB, service data = 2 B header + 8 B privateId
  (the app takes hex characters 4..20). Scan filter in the app: service data prefix [0x00, 0x30] with a mask.
- privateIdVersion: 0 = old Tile (service data directly contains the tileId), 1 = unused, 2 = the hash scheme above.
- auth_key + tile_uuid come from the cloud: GET tiles/{tile_uuid} (TileResourceEntry.auth_key).
- Scan filter for "FEEC"/iBeacon (apple mfr 76, uuid 9d410000-35d6-f4dd-ba60-e7bd8dc491c0) - pre-activation/other mode.
- Not yet verified against a live advertisement (needs: auth_key + BLE capture).

## VERIFIED LIVE (2026-11-11, tools/scan_verify.py, bleak on a desktop)
- 4 of 5 trackers matched a `privateId` derived from authKey (v2) within a 40 s scan. The algorithm above is correct.
- Service data 0xFEED = 10 B: byte0 = 0x02 (version), byte1 = flags (0x00 observed), byte2..9 = privateId.
- ROYAL_ST1 (fw 48.x, 2022): has a static MAC (= tileId bytes), but the privateId rotates.
- ATM24_MATE1 (fw 59.x, 2024) and others: the MAC rotates (random/non-resolvable) and so does the privateId → match
  by privateId, not by MAC. ctr is the position within the 15-min cycle phase (different per tile).
- Search window: ctr around "now" is enough; the full table of 8641 values per tile is cheap (~ms).
