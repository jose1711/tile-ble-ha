import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components", "tile_ble"))
import toa_protocol as p  # noqa: E402


def test_tcu_low_duty_matches_live_capture():
    # Captured live (ring_direct.mjs): "020c032001300104005802..."
    # -> TOA prefix 0x0c, subprefix 03, params follow.
    assert p.TcuLowDuty().to_bytes().hex() == "2001300104005802"


def test_pre_auth_frame_matches_live_capture():
    # Captured live: "TX preAuth 009468bf401301"
    assert p.build_pre_auth_frame(19, bytes([1]), bytes.fromhex("9468bf40")).hex() == "009468bf401301"


def test_feature_bit_layout():
    # TFC = byte 2, bit 0; TCU = byte 1, bit 4 - verifies the gap for byte_location > 2.
    assert p.feature_supported(bytes([0, 0, 0b00000001, 0]), "TFC") is True
    assert p.feature_supported(bytes([0, 0b00010000, 0, 0]), "TCU") is True
    assert p.feature_supported(bytes([0, 0, 0, 0]), "TFC") is False


def test_parse_mep_response_connectionless():
    token = bytes.fromhex("9468bf40")
    raw = bytes([0]) + token + bytes([20, 0x01, 0x3f])
    frame = p.parse_mep_response(raw, token, None)
    assert frame.kind == "CONNECTIONLESS"
    assert frame.toa_prefix == 20
    assert frame.data == bytes([0x01, 0x3f])


def test_parse_mep_response_cid():
    raw = bytes([2, 14, 3, 0xaa, 0xbb, 0xcc, 0xdd])
    frame = p.parse_mep_response(raw, bytes(4), channel_prefix=2)
    assert frame.kind == "CID"
    assert frame.toa_prefix == 14


def test_clean_toa_bytes_strips_mic_only_when_not_connectionless():
    data = bytes(range(10))
    assert p.clean_toa_bytes(False, data) == data[:-4]
    assert p.clean_toa_bytes(True, data) == data
