#!/usr/bin/env python3
"""Derivation of the Tile privateId (version 2) - based on the decompiled app 2.148.0 (p334pd/a.java).

  authKey   : 16 B (cloud: base64)
  tileId    : 8 B  (cloud tile_uuid, hex)
  seed      = HMAC-SHA256(authKey, pad32(tileId || b"identity"))[:16]
  privateId = HMAC-SHA256(seed,    pad32(ctr as uint32 LE))[:8],  ctr = 0..8640
Advertisement: service data 0xFEED = 2 B header + 8 B privateId.
"""
import base64, hashlib, hmac, struct

def _hmac(key: bytes, msg: bytes) -> bytes:
    assert len(msg) <= 32
    return hmac.new(key, msg.ljust(32, b"\0"), hashlib.sha256).digest()

def private_ids(auth_key_b64: str, tile_id_hex: str, count: int = 8641) -> dict[bytes, int]:
    auth_key = base64.b64decode(auth_key_b64)
    seed = _hmac(auth_key, bytes.fromhex(tile_id_hex) + b"identity")[:16]
    return {_hmac(seed, struct.pack("<I", c))[:8]: c for c in range(count)}

def match(service_data: bytes, table: dict[bytes, int]):
    """service_data = value under UUID 0000FEED-...; returns the counter or None."""
    return table.get(service_data[2:10])
