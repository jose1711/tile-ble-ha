"""Minimal Tile cloud client - only a one-time download of tile_uuid + auth_key.

The password is never stored. Endpoints follow pytile / the app.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from aiohttp import ClientError, ClientSession, ClientTimeout

_LOGGER = logging.getLogger(__name__)

API = "https://production.tile-api.com/api/v1"
HEADERS = {
    "User-Agent": "Tile/4774 CFNetwork/1312 Darwin/21.0.0",
    "tile_api_version": "1.0",
    "tile_app_id": "ios-tile-production",
    "tile_app_version": "2.89.1.4774",
}


class TileAuthError(Exception):
    """Invalid credentials."""


class TileRequestError(Exception):
    """Connection / API error."""


async def _req(session: ClientSession, method: str, path: str, client_uuid: str, **kw: Any) -> dict:
    headers = {**HEADERS, "tile_client_uuid": client_uuid}
    try:
        async with session.request(method, f"{API}/{path}", headers=headers, timeout=ClientTimeout(total=15), **kw) as r:
            if r.status in (401, 403):
                raise TileAuthError
            r.raise_for_status()
            return await r.json()
    except (ClientError, TimeoutError, ValueError) as err:  # ValueError: invalid JSON body
        raise TileRequestError(str(err) or type(err).__name__) from err


async def fetch_tiles(session: ClientSession, email: str, password: str) -> tuple[str, list[dict[str, Any]]]:
    """Return (user_uuid, [{tile_uuid, name, product, archetype, auth_key}, ...]) for trackers that have an auth_key."""
    cu = str(uuid4())
    await _req(session, "put", f"clients/{cu}", cu,
               data={"app_id": HEADERS["tile_app_id"], "app_version": HEADERS["tile_app_version"],
                     "locale": "en-US"})
    sess = await _req(session, "post", f"clients/{cu}/sessions", cu, data={"email": email, "password": password})
    try:
        user_uuid = sess["result"]["user"]["user_uuid"]
        states = await _req(session, "get", "tiles/tile_states", cu)
        tile_ids = [st["tile_id"] for st in states["result"]]
    except (KeyError, TypeError) as err:
        raise TileRequestError(f"unexpected API response: {err!r}") from err
    out: list[dict[str, Any]] = []
    for tile_id in tile_ids:
        try:
            raw = (await _req(session, "get", f"tiles/{tile_id}", cu))["result"]
        except (TileRequestError, KeyError, TypeError):
            continue  # e.g. Tile Labels (HTTP 412)
        if raw.get("auth_key"):
            entry = {k: raw.get(k) for k in ("tile_uuid", "name", "product", "archetype", "auth_key")}
            firmware = raw.get("firmware") or {}
            entry["expected_tdt_cmd_config"] = firmware.get("expected_tdt_cmd_config")
            out.append(entry)
    return user_uuid, out


# Mapping per Tile.BatteryStatus (com/tile/android/data/table/Tile.java):
#   "NONE" -> HEALTHY, "LEVEL2" -> LOW, "LEVEL1" -> DEAD.
BATTERY_STATUS_MAP = {"NONE": "healthy", "LEVEL2": "low", "LEVEL1": "dead"}


async def fetch_battery_statuses(session: ClientSession, email: str, password: str,
                                  tile_uuids: list[str]) -> dict[str, str]:
    """One-time sign-in + read of battery_status for the given tile_uuids.

    Returns {tile_uuid: "healthy"|"low"|"dead"|"unknown"}. Call rarely (e.g. once per 24h) -
    this is the only part of the integration that needs the cloud (presence and ringing are local).
    """
    cu = str(uuid4())
    await _req(session, "put", f"clients/{cu}", cu,
               data={"app_id": HEADERS["tile_app_id"], "app_version": HEADERS["tile_app_version"],
                     "locale": "en-US"})
    await _req(session, "post", f"clients/{cu}/sessions", cu, data={"email": email, "password": password})
    out: dict[str, str] = {}
    for tile_uuid in tile_uuids:
        try:
            raw = (await _req(session, "get", f"tiles/{tile_uuid}", cu))["result"]
        except (TileRequestError, KeyError, TypeError) as err:
            _LOGGER.debug("battery_status for %s unavailable: %r", tile_uuid, err)
            continue
        out[tile_uuid] = BATTERY_STATUS_MAP.get(raw.get("battery_status"), "unknown")
    return out
