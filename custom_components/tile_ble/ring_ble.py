"""Connect + authenticate + ring a Tile tracker via bleak.

Port of the state machine from node-tile AbstractTileService/TileServiceBle and from
the decompiled app (Zd/k.java). Requires `authKey` (base64) and `tile_uuid` (hex)
from the Tile cloud and `expected_tdt_cmd_config` (base64, from the firmware.expected_tdt_cmd_config field).
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import struct

from bleak import BleakClient
from bleak.backends.device import BLEDevice

try:  # available inside Home Assistant (bluetooth integration dependency)
    from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
except ImportError:  # standalone use (tools/, tests)
    establish_connection = None  # type: ignore[assignment]

try:
    from . import toa_protocol as p
except ImportError:  # also allows importing directly outside the package (test scripts)
    import toa_protocol as p  # type: ignore[import-not-found]

_LOGGER = logging.getLogger(__name__)


class RingError(Exception):
    pass


class TileRinger:
    def __init__(self, address: str | BLEDevice, auth_key_b64: str, tile_uuid_hex: str,
                 expected_tdt_cmd_config_b64: str | None = None):
        self.address = address
        self.auth_key = base64.b64decode(auth_key_b64)
        self.tile_uuid = tile_uuid_hex
        self.expected_tdt = (base64.b64decode(expected_tdt_cmd_config_b64)
                              if expected_tdt_cmd_config_b64 else None)

        self.client: BleakClient | None = None
        self.local_token = os.urandom(4)
        self.rand_a: bytes = b""
        self.channel_prefix: int | None = None
        self.channel_data: bytes = b""
        self.auth_key_hmac: bytes | None = None
        self.nonce_a = 0
        self.features_available: bytes | None = None
        self.tile_id: str | None = None

        self._pending: dict[int, asyncio.Future] = {}
        self._tasks: set[asyncio.Task] = set()
        self._auth_done: asyncio.Future = asyncio.get_running_loop().create_future()

    # --- low-level I/O -----------------------------------------------------
    async def _write(self, data: bytes) -> None:
        await self.client.write_gatt_char(p.MEP_COMMAND_CHAR, data, response=False)

    def _on_notify(self, _handle, raw: bytes) -> None:
        frame = p.parse_mep_response(bytes(raw), self.local_token, self.channel_prefix)
        if frame.kind == "INVALID" or frame.toa_prefix is None:
            return
        clean = p.clean_toa_bytes(frame.kind == "CONNECTIONLESS", frame.data)
        fut = self._pending.pop(frame.toa_prefix, None)
        if fut and not fut.done():
            fut.set_result(clean)
            return
        # unsolicited (notification) messages are handled asynchronously
        self._spawn(self._handle_unsolicited(frame.toa_prefix, clean))

    def _spawn(self, coro) -> None:
        """Run a background coroutine, keeping a reference and surfacing its failure."""
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        if (err := task.exception()) is not None:
            _LOGGER.debug("background handler failed: %r", err)
            self._fail_all(RingError(f"handshake step failed: {err!r}"))

    def _fail_all(self, err: Exception) -> None:
        """Fail every pending reply and the handshake itself."""
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(err)
        self._pending.clear()
        if not self._auth_done.done():
            self._auth_done.set_exception(err)

    def _on_disconnect(self, _client) -> None:
        self._fail_all(RingError("tracker disconnected"))

    async def _write_and_wait(self, frame: bytes, wait_prefix: int | None,
                               timeout: float) -> bytes | None:
        """Write `frame`; if `wait_prefix` is given, return the reply with that TOA prefix.

        The reply future is registered BEFORE the write, so a fast reply cannot be missed.
        """
        if wait_prefix is None:
            await self._write(frame)
            return None
        fut = asyncio.get_running_loop().create_future()
        self._pending[wait_prefix] = fut
        try:
            await self._write(frame)
            return await asyncio.wait_for(fut, timeout)
        finally:
            if self._pending.get(wait_prefix) is fut:
                del self._pending[wait_prefix]

    async def _send_pre_auth(self, toa_prefix: int, data: bytes, wait_prefix: int | None = None,
                              timeout: float = 10.0) -> bytes | None:
        frame = p.build_pre_auth_frame(toa_prefix, data, self.local_token)
        return await self._write_and_wait(frame, wait_prefix, timeout)

    async def _send_cid(self, toa_prefix: int, data: bytes, wait_prefix: int | None = None,
                         timeout: float = 10.0) -> bytes | None:
        self.nonce_a += 1
        frame = p.build_cid_frame(toa_prefix, data, self.channel_prefix, self.nonce_a,
                                   self.auth_key_hmac)
        return await self._write_and_wait(frame, wait_prefix, timeout)

    async def _handle_unsolicited(self, toa_prefix: int, data: bytes) -> None:
        name = p.TOA_RSP.get(toa_prefix)
        if name == "TDG":
            # diagnostic dump - the last chunk has prefix==1 (after stripping the MIC the first byte
            # is the original "sub-prefix"; our clean_toa_bytes returns [subprefix, ...payload])
            if data and data[0] == 1:
                await self._after_tdg()
        elif name == "TKA":
            if data and data[0] == 3:  # TKA_RSP_CHECK -> heartbeat response
                await self._send_cid(8, bytes([3]))
        elif name == "TRM":
            pass  # RSSI monitoring notifications - not needed for ringing
        else:
            _LOGGER.debug("unhandled TOA notification prefix=%s data=%s", toa_prefix, data.hex())

    # --- TDI (identification data, before authentication) ------------------------
    async def _tdi_sequence(self) -> None:
        avail = await self._send_pre_auth(19, bytes([1]), wait_prefix=20)
        avail_byte = avail[0] if avail else 0
        for name, (bit, req_prefix) in p.TDI_FIELDS.items():
            if p.tdi_is_available(avail_byte, bit):
                resp = await self._send_pre_auth(19, bytes([req_prefix]), wait_prefix=20)
                value = resp[1:] if resp else b""
                if name == "TILE_ID":
                    self.tile_id = value.hex()
                _LOGGER.debug("TDI %s = %s", name, value)

    # --- Authentication (randA/randT/sresT, channel opening) --------------------
    async def _authenticate(self) -> None:
        await self._tdi_sequence()

        self.rand_a = os.urandom(14)
        auth_resp = await self._send_pre_auth(20, self.rand_a, wait_prefix=21)
        # auth_resp = randT(10B) + sresT(4B); no need to verify them on the client (the server does)
        del auth_resp

        open_resp = await self._write_and_wait(
            p.build_pre_auth_frame(16, self.rand_a, self.local_token), 18, 10.0)
        # open_resp = [channelPrefix, ...channelData], per the JS handleChannelOpenTransaction
        # (ChOpenTransaction: prefix=data[0], data=data[1:]).
        self.channel_prefix = open_resp[0]
        self.channel_data = open_resp[1:]

        self.auth_key_hmac = p.generate_hmac(
            self.auth_key, self.rand_a, self.channel_data, self.channel_prefix, self.local_token
        )[:16]

        # acknowledge the channel opening (JS: sendPackets(18, [19]))
        ready = await self._send_cid(18, bytes([19]), wait_prefix=1)
        self._parse_ready(ready)

        if p.feature_supported(self.features_available, "TFC"):
            await self._send_cid(15, bytes([1]))
        elif p.feature_supported(self.features_available, "TDG"):
            await self._send_cid(10, bytes([1]), wait_prefix=12)
        else:
            raise RingError("Neither TFC nor TDG is supported - cannot continue")

    def _parse_ready(self, data: bytes) -> None:
        # TOA_RSP_READY: [maxPayloadSize, ...nonceData]; nonceData[0:3]=info, [3:7]=nonceB (LE u32),
        # the rest = featuresAvailable (together with info if nonceData is shorter than 7 B)
        nonce_data = data[1:]
        if len(nonce_data) >= 7:
            info = nonce_data[0:3]
            rest = nonce_data[7:]
            self.features_available = info + rest if rest else info
        else:
            self.features_available = nonce_data

    async def _after_tdg(self) -> None:
        if p.feature_supported(self.features_available, "PPM"):
            await self._send_cid(6, bytes([]))
        if p.feature_supported(self.features_available, "ADV_INT"):
            await self._send_cid(7, bytes([2]), wait_prefix=9)
        if p.feature_supported(self.features_available, "TCU"):
            await self._send_tcu_request()
        else:
            await self._finish_auth()

    async def _send_tcu_request(self) -> None:
        params = p.TcuLowDuty().to_bytes()
        resp = await self._send_cid(12, bytes([3]) + params, wait_prefix=14, timeout=15)
        # resp[0] == 3 -> OK; any response (even an error) means we can finish the handshake
        del resp
        await self._finish_auth()

    async def _finish_auth(self) -> None:
        """Port of notifyAuthTripletSeen(): optional TDT/TIME/TPFS exchanges before finishing the handshake."""
        if p.feature_supported(self.features_available, "TDT") and self.expected_tdt:
            tdt_payload = bytes([1]) + self.expected_tdt
            await self._write(p.build_cid_frame(4, tdt_payload, self.channel_prefix,
                                                 self._next_nonce(), self.auth_key_hmac))
        if p.feature_supported(self.features_available, "TIME"):
            import time as _time
            unix_epoch_2016 = 1451606400
            cur_time = int(_time.time()) - unix_epoch_2016
            time_payload = bytes([3]) + struct.pack("<q", cur_time)
            await self._write(p.build_cid_frame(13, time_payload, self.channel_prefix,
                                                 self._next_nonce(), self.auth_key_hmac))
        if p.feature_supported(self.features_available, "TPFS"):
            try:
                await self._send_cid(5, bytes([6]), wait_prefix=7, timeout=5)
            except asyncio.TimeoutError:
                _LOGGER.debug("TPFS song-map request timeout (harmless)")
        if not self._auth_done.done():
            self._auth_done.set_result(True)

    def _next_nonce(self) -> int:
        self.nonce_a += 1
        return self.nonce_a

    # --- public API -------------------------------------------------------------
    async def connect(self, timeout: float = 35.0) -> None:
        if establish_connection is not None and isinstance(self.address, BLEDevice):
            # retries transient failures and handles proxy connection slots
            self.client = await establish_connection(
                BleakClientWithServiceCache, self.address, self.address.address,
                disconnected_callback=self._on_disconnect, max_attempts=3)
        else:
            self.client = BleakClient(self.address, disconnected_callback=self._on_disconnect)
            await self.client.connect(timeout=timeout)
        await self.client.start_notify(p.MEP_RESPONSE_CHAR, self._on_notify)

    async def authenticate(self, timeout: float = 25.0) -> None:
        await self._authenticate()
        try:
            await asyncio.wait_for(asyncio.shield(self._auth_done), timeout)
        except asyncio.TimeoutError as err:
            raise RingError("handshake did not complete in time "
                            "(tracker never sent the final diagnostics chunk)") from err

    async def ring(self, volume: bytes = p.VOLUME_MED, seconds: int = 5) -> None:
        payload = volume
        if p.feature_supported(self.features_available, "TSD"):
            payload = payload + bytes([seconds])
        await self._send_cid(5, bytes([2]) + payload, wait_prefix=7, timeout=10)

    async def disconnect(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        for fut in self._pending.values():
            fut.cancel()
        self._pending.clear()
        if self.client and self.client.is_connected:
            await self.client.disconnect()


async def ring_tile(address: str, auth_key_b64: str, tile_uuid_hex: str,
                     expected_tdt_cmd_config_b64: str | None = None,
                     volume: bytes = p.VOLUME_MED, seconds: int = 5) -> None:
    ringer = TileRinger(address, auth_key_b64, tile_uuid_hex, expected_tdt_cmd_config_b64)
    try:
        await ringer.connect()
        await ringer.authenticate()
        await ringer.ring(volume, seconds)
        await asyncio.sleep(seconds + 2)
    finally:
        await ringer.disconnect()
