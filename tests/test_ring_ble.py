import asyncio, os, sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components", "tile_ble"))
import ring_ble  # noqa: E402
import toa_protocol as p  # noqa: E402


class FakeClient:
    """Replies to a write synchronously, i.e. before the write call returns."""

    def __init__(self, ringer):
        self.ringer = ringer

    async def write_gatt_char(self, _char, data, response=False):
        raw = bytes([0]) + self.ringer.local_token + bytes([20, 0x01])
        self.ringer._on_notify(None, raw)
        await asyncio.sleep(0)


def _ringer():
    r = ring_ble.TileRinger("AA:BB:CC:DD:EE:FF", "AAAAAAAAAAAAAAAAAAAAAA==", "0011223344556677")
    r.client = FakeClient(r)
    return r


@pytest.mark.asyncio
async def test_fast_reply_during_write_is_not_lost():
    r = _ringer()
    assert await r._send_pre_auth(19, bytes([1]), wait_prefix=20, timeout=1) == bytes([0x01])
    assert not r._pending


@pytest.mark.asyncio
async def test_disconnect_fails_pending_and_handshake():
    r = _ringer()
    fut = asyncio.get_running_loop().create_future()
    r._pending[99] = fut
    r._on_disconnect(None)
    with pytest.raises(ring_ble.RingError):
        await fut
    with pytest.raises(ring_ble.RingError):
        await r._auth_done
