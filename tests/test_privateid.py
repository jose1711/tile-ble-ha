import base64, json, os, sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components", "tile_ble"))
from privateid import extract_private_id, private_ids  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..", "secrets")


def test_table_size_and_lookup():
    key = base64.b64encode(bytes(range(16))).decode()
    t = private_ids(key, "0011223344556677")
    assert len(t) == 8641
    pid, ctr = next(iter(t.items()))
    assert len(pid) == 8 and t[pid] == ctr


def test_extract():
    assert extract_private_id(bytes([2, 0]) + b"A" * 8) == b"A" * 8
    assert extract_private_id(bytes([0, 0]) + b"A" * 8) is None
    assert extract_private_id(b"\x02\x00") is None


@pytest.mark.skipif(not os.path.exists(f"{ROOT}/maclog.jsonl"), reason="no live data")
def test_live_captures():
    """Every captured advertisement from maclog.jsonl must match the table (local only)."""
    tiles = {t["name"]: t for t in json.load(open(f"{ROOT}/tiles.json")) if t["auth_key"]}
    tables = {n: private_ids(t["auth_key"], t["tile_uuid"]) for n, t in tiles.items()}
    rows = [json.loads(l) for l in open(f"{ROOT}/maclog.jsonl")]
    assert rows
    for r in rows:
        assert tables[r["name"]][extract_private_id(bytes.fromhex(r["sd"]))] == r["ctr"]
