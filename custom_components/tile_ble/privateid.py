"""Derivation of the Tile privateId (version 2) - based on the decompiled app 2.148.0.

  seed      = HMAC-SHA256(authKey, pad32(tileId[8] || b"identity"))[:16]
  privateId = HMAC-SHA256(seed,    pad32(uint32 LE ctr))[:8],  ctr = 0..8640
Service data 0xFEED = [version][flags][privateId 8 B].
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import struct

COUNTER_MAX = 8641
VERSION_HASHED = 2


def _hmac(key: bytes, msg: bytes) -> bytes:
    return hmac.new(key, msg.ljust(32, b"\0"), hashlib.sha256).digest()


def private_ids(auth_key_b64: str, tile_id_hex: str, count: int = COUNTER_MAX) -> dict[bytes, int]:
    """Return {privateId: ctr} for all values of the cycle (blocking, ~10 ms)."""
    auth_key = base64.b64decode(auth_key_b64)
    seed = _hmac(auth_key, bytes.fromhex(tile_id_hex) + b"identity")[:16]
    return {_hmac(seed, struct.pack("<I", c))[:8]: c for c in range(count)}


def extract_private_id(service_data: bytes) -> bytes | None:
    """Extract the privateId from service data; None if it is not version 2."""
    if len(service_data) < 10 or service_data[0] != VERSION_HASHED:
        return None
    return bytes(service_data[2:10])
