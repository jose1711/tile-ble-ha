"""Tile TOA/MEP protocol - pure derivation/parsing, no BLE I/O.

Port of node-tile (Lesley De Keyser, https://www.npmjs.com/package/node-tile) and of
the decompiled app com.thetileapp.tile 2.148.0 (p081db/*, Zd/k.java, Vg/*).
Verified live (ring_all.mjs) on 5 trackers: ROYAL_ST1, ROYAL_ATM1, ATM24_MATE1.
"""
from __future__ import annotations

import hmac
import hashlib
import struct
from dataclasses import dataclass, field

FEED_SERVICE = "0000feed-0000-1000-8000-00805f9b34fb"
MEP_COMMAND_CHAR = "9d410018-35d6-f4dd-ba60-e7bd8dc491c0"
MEP_RESPONSE_CHAR = "9d410019-35d6-f4dd-ba60-e7bd8dc491c0"
TILE_ID_CHAR = "9d410007-35d6-f4dd-ba60-e7bd8dc491c0"

# preAuth (connectionless) frames may only use these TOA prefixes.
PRE_AUTH_PREFIXES = (16, 19, 20, 21)

TOA_RSP = {
    1: "READY", 6: "TDT", 7: "SONG", 8: "PPM", 9: "ADV_INT", 10: "TKA",
    12: "TDG", 14: "TCU", 15: "TIME", 18: "OPEN_CHANNEL", 19: "CLOSE_CHANNEL",
    20: "TDI", 21: "AUTHENTICATE", 25: "TRM", 27: "ASSOCIATE",
}


def generate_hmac(secret: bytes, *parts: bytes | int) -> bytes:
    """HMAC-SHA256 over the concatenation of `parts`, zero-padded to 32 B (like Zg.c.d / CryptoUtils.generateHmac)."""
    bufs = [p if isinstance(p, bytes) else bytes([p]) for p in parts]
    msg = b"".join(bufs)
    if len(msg) > 32:
        raise ValueError("msg > 32 B")
    return hmac.new(secret, msg.ljust(32, b"\0"), hashlib.sha256).digest()


def long_le(n: int) -> bytes:
    return struct.pack("<q", n)


# --- Tile Data Information (TDI) --------------------------------------------------
TDI_FIELDS = {"TILE_ID": (0, 2), "FIRMWARE": (1, 3), "MODEL": (2, 4), "HARDWARE": (3, 5)}


def tdi_is_available(avail_byte: int, bit_index: int) -> bool:
    return bool(avail_byte & (1 << bit_index))


# --- ToaFeature bitmap (featuresAvailable z TOA_RSP_READY) -------------------------
# (byte_location, bit_location)
FEATURES = {
    "TOFU": (0, 0), "BDADDR": (0, 1), "TDT": (0, 2), "SONG": (0, 3), "PPM": (0, 4),
    "ADV_INT": (0, 5), "OLD_TKA": (0, 6), "TAC": (0, 7),
    "TKA": (1, 1), "TDG": (1, 2), "TMD": (1, 3), "TCU": (1, 4), "TIME": (1, 5),
    "TEST": (1, 6), "TMA": (1, 7),
    "TFC": (2, 0), "TPFS": (2, 1), "TRM": (2, 5), "TPC": (2, 6),
    "TPI": (3, 0), "TSD": (3, 1), "TUC": (3, 2),
}


def feature_supported(features_available: bytes | None, name: str) -> bool:
    if not features_available:
        return False
    byte_loc, bit_loc = FEATURES[name]
    if byte_loc > 2:
        byte_loc += 1  # same gap as in ToaProcessor.isToaFeatureSupported
    if byte_loc >= len(features_available):
        return False
    return bool(features_available[byte_loc] & (1 << bit_loc))


# --- TileVolume ----------------------------------------------------------------
VOLUME_LOW = bytes([1, 1])
VOLUME_MED = bytes([1, 2])
VOLUME_HIGH = bytes([1, 3])


@dataclass
class TcuLowDuty:
    """LowDuty TCU parameters (min/max conn interval, slave latency, conn sup timeout)."""
    min_conn_interval: int = 288
    max_conn_interval: int = 304
    slave_latency: int = 4
    conn_sup_timeout: int = 600

    def to_bytes(self) -> bytes:
        return struct.pack("<HHHH", self.min_conn_interval, self.max_conn_interval,
                            self.slave_latency, self.conn_sup_timeout)


@dataclass
class MepFrame:
    """A single parsed frame received from MEP_RESPONSE_CHAR."""
    kind: str  # "CONNECTIONLESS" | "CID" | "INVALID"
    toa_prefix: int | None = None
    data: bytes = b""


def parse_mep_response(raw: bytes, local_token: bytes, channel_prefix: int | None) -> MepFrame:
    """Port ToaMepProcessor.getResponseType + createTransaction."""
    if len(raw) == 0:
        return MepFrame("INVALID")
    prefix = raw[0]
    if prefix == 0:
        if len(raw) < 5:
            return MepFrame("INVALID")
        first4 = raw[1:5]
        if first4 == b"\xff\xff\xff\xff" or first4 == local_token:
            toa = raw[5:]
            return MepFrame("CONNECTIONLESS", toa[0] if toa else None, toa[1:] if toa else b"")
        return MepFrame("INVALID")
    if channel_prefix is not None and prefix == channel_prefix:
        toa = raw[1:]
        return MepFrame("CID", toa[0] if toa else None, toa[1:] if toa else b"")
    return MepFrame("INVALID")


def clean_toa_bytes(is_connectionless: bool, data: bytes) -> bytes:
    """After the channel is opened, every CID message carries a 4 B HMAC-MIC at the end that must be stripped."""
    if is_connectionless:
        return data
    return data[:-4] if len(data) >= 4 else data


def build_pre_auth_frame(toa_prefix: int, data: bytes, local_token: bytes) -> bytes:
    if toa_prefix not in PRE_AUTH_PREFIXES:
        raise ValueError(f"invalid preAuth prefix {toa_prefix}")
    return bytes([0]) + local_token + bytes([toa_prefix]) + data


def build_cid_frame(toa_prefix: int, data: bytes, channel_prefix: int, nonce_a: int,
                     auth_key_hmac: bytes) -> bytes:
    """Port AbstractTileService.sendPackets (MEP, post-open-channel)."""
    payload = bytes([toa_prefix]) + data
    mic = generate_hmac(auth_key_hmac, long_le(nonce_a), 1, len(payload), payload)[:4]
    payload_with_mic = payload + mic
    return bytes([channel_prefix]) + payload_with_mic
