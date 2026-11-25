"""iBeacons tracked alongside Tiles (same presence/RSSI/device_tracker entities, incl. mobile scanners).

Configured in Options, one beacon per line: ``Name, UUID[, major[, minor]]``. An omitted major/minor
matches any value. HA's own Bluetooth adapters/proxies are listened to directly (zone.home); phones and
other scanners report them through tile_ble.report_ibeacon.
"""
from __future__ import annotations

import uuid as uuidlib

APPLE_COMPANY_ID = 0x004C
IBEACON_PREFIX = b"\x02\x15"
IBEACON_PRODUCT = "iBeacon"


class IBeaconConfigError(ValueError):
    """A line of the iBeacon list cannot be parsed."""


def normalize_uuid(value: str) -> str:
    return str(uuidlib.UUID(value.strip()))  # raises ValueError


def beacon_key(uuid: str, major: int | None, minor: int | None) -> str:
    """Stable id used as the device identifier / unique_id prefix (the 'tile_uuid' of a beacon)."""
    key = f"ibeacon_{uuid.replace('-', '')}"
    if major is not None:
        key += f"_{major}"
        if minor is not None:
            key += f"_{minor}"
    return key


def parse_config(text: str) -> list[dict]:
    """Parse the Options text into beacon dicts; raises IBeaconConfigError naming the bad line."""
    beacons: list[dict] = []
    seen: set[str] = set()
    for lineno, raw in enumerate((text or "").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        try:
            if len(parts) < 2 or len(parts) > 4 or not parts[0]:
                raise ValueError("expected: Name, UUID[, major[, minor]]")
            uuid = normalize_uuid(parts[1])
            major = int(parts[2]) if len(parts) > 2 and parts[2] != "" else None
            minor = int(parts[3]) if len(parts) > 3 and parts[3] != "" else None
            if minor is not None and major is None:
                raise ValueError("minor needs a major")
            for v in (major, minor):
                if v is not None and not 0 <= v <= 0xFFFF:
                    raise ValueError("major/minor must be 0-65535")
        except ValueError as err:
            raise IBeaconConfigError(f"line {lineno}: {err}") from err
        key = beacon_key(uuid, major, minor)
        if key in seen:
            raise IBeaconConfigError(f"line {lineno}: duplicate beacon")
        seen.add(key)
        beacons.append({"tile_uuid": key, "name": parts[0], "product": IBEACON_PRODUCT,
                        "manufacturer": IBEACON_PRODUCT, "uuid": uuid, "major": major, "minor": minor})
    return beacons


def parse_advertisement(manufacturer_data: bytes) -> tuple[str, int, int, int] | None:
    """Apple manufacturer data (without the company id) -> (uuid, major, minor, tx_power) or None."""
    if len(manufacturer_data) < 23 or manufacturer_data[:2] != IBEACON_PREFIX:
        return None
    d = manufacturer_data
    uuid = str(uuidlib.UUID(bytes=bytes(d[2:18])))
    tx = d[22] - 256 if d[22] > 127 else d[22]
    return uuid, int.from_bytes(d[18:20], "big"), int.from_bytes(d[20:22], "big"), tx


def build_lookup(beacons: list[dict]) -> dict[tuple[str, int | None, int | None], str]:
    return {(b["uuid"], b["major"], b["minor"]): b["tile_uuid"] for b in beacons}


def match(lookup: dict, uuid: str, major: int, minor: int) -> str | None:
    """Most specific configured beacon for this advertisement."""
    return lookup.get((uuid, major, minor)) or lookup.get((uuid, major, None)) or lookup.get((uuid, None, None))
