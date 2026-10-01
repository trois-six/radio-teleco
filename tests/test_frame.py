"""868 MHz radio frame model.

The rolling-code vectors below use a throwaway serial (``0xABCDEF``) and a throwaway seed
constant (``TEST_SEED_XOR``, set in conftest.py), never real ones: they pin the
reverse-engineered algorithm (layout, rotations, step keys, output mask), not any
device's or box's data.
"""

from __future__ import annotations

import pytest

from radio_teleco.frame import (
    ROLLING_CODE_MAX_COUNTER,
    SEED_XOR_ENV,
    UNIT_US,
    RadioFrame,
    SeedXorMissingError,
    frame_checksum,
    invert_rolling_code,
    rolling_code,
    seed_xor,
    seed_xor_from,
)

from .conftest import TEST_SEED_XOR

ROLLING = bytes.fromhex("0123456789")
TEST_SERIAL = 0xABCDEF
# (counter, expected frame for channel 1), spanning every counter bit at least once.
ROLLING_VECTORS = [
    (0, "41 88 04 00 C0 00 01 72"),
    (1, "63 5C 10 A1 42 00 01 4D"),
    (2, "65 E4 10 D2 42 00 01 92"),
    (7, "16 50 30 40 C0 00 01 69"),
    (8, "A1 EC 30 73 46 00 01 89"),
    (15, "BE 68 25 E3 06 00 01 CB"),
    (16, "00 78 24 23 C5 00 01 7B"),
    (255, "B7 F8 0E F2 C9 00 01 87"),
    (256, "29 A4 11 11 52 00 01 BE"),
    (300, "98 80 02 81 90 00 01 D4"),
    (511, "DE 6C 2E F1 9F 00 01 F7"),
    (512, "28 EC 10 53 A6 00 01 E2"),
    (513, "4A F4 35 02 E4 00 01 A6"),
    (600, "C8 38 0C F3 27 00 01 D9"),
    (768, "41 38 24 D3 F6 00 01 99"),
    (1023, "F7 C4 0B 52 39 00 01 AE"),
]


def test_build_adds_the_checksum() -> None:
    frame = RadioFrame.build(ROLLING, 7)
    assert frame.data[:7] == ROLLING + b"\x00\x07"
    assert sum(frame.data) % 256 == 0
    assert frame.data[7] == frame_checksum(frame.data)
    assert (frame.rolling_code, frame.channel, frame.valid) == (ROLLING, 7, True)
    assert str(frame) == "01 23 45 67 89 00 07 " + f"{frame.data[7]:02X}"


def test_from_hex_and_validity() -> None:
    good = RadioFrame.build(ROLLING, 1)
    assert RadioFrame.from_hex(good.data.hex()) == good
    bad_sum = RadioFrame(good.data[:7] + bytes(((good.data[7] + 1) & 0xFF,)))
    assert not bad_sum.valid
    reserved = bytearray(good.data)
    reserved[5], reserved[7] = 0x10, (reserved[7] - 0x10) & 0xFF
    assert sum(reserved) % 256 == 0
    assert not RadioFrame(bytes(reserved)).valid


def test_segments_round_trip() -> None:
    frame = RadioFrame.build(ROLLING, 8)
    segments = frame.segments_us()
    assert len(segments) == 64
    assert set(segments) <= {UNIT_US, 2 * UNIT_US}
    assert segments[:8] == [UNIT_US] * 7 + [2 * UNIT_US]  # 0x01, MSB first
    jittered = [s + (30 if i % 2 else -30) for i, s in enumerate(segments)]
    assert RadioFrame.from_segments(jittered) == frame


def test_invalid_inputs() -> None:
    with pytest.raises(ValueError, match="8 bytes"):
        RadioFrame(b"\x00" * 7)
    with pytest.raises(ValueError, match="5 bytes"):
        RadioFrame.build(b"\x00", 1)
    with pytest.raises(ValueError, match="channel"):
        RadioFrame.build(ROLLING, 0)
    with pytest.raises(ValueError, match="64 segments"):
        RadioFrame.from_segments([UNIT_US] * 63)
    with pytest.raises(ValueError, match="neither"):
        RadioFrame.from_segments([UNIT_US] * 63 + [3 * UNIT_US])


# --- rolling code -----------------------------------------------------------------------


@pytest.mark.parametrize(("counter", "expected"), ROLLING_VECTORS)
def test_rolling_code_matches_the_reverse_engineered_algorithm(counter: int, expected: str) -> None:
    assert str(RadioFrame.from_serial(TEST_SERIAL, counter, 1)) == expected


def test_rolling_code_is_the_frames_first_five_bytes() -> None:
    frame = RadioFrame.from_serial(TEST_SERIAL, 42, 3)
    assert frame.rolling_code == rolling_code(TEST_SERIAL, 42)
    assert frame.channel == 3
    assert frame.valid


def test_rolling_code_covers_counter_bits_0_to_9_only() -> None:
    assert ROLLING_CODE_MAX_COUNTER == 1023
    with pytest.raises(ValueError, match=r"0\.\.1023"):
        rolling_code(TEST_SERIAL, 1024)


def test_rolling_code_invalid_inputs() -> None:
    with pytest.raises(ValueError, match="24-bit"):
        rolling_code(1 << 24, 0)
    with pytest.raises(ValueError, match="24-bit"):
        rolling_code(-1, 0)
    with pytest.raises(ValueError, match=f"0..{ROLLING_CODE_MAX_COUNTER}"):
        rolling_code(TEST_SERIAL, ROLLING_CODE_MAX_COUNTER + 1)
    with pytest.raises(ValueError, match=f"0..{ROLLING_CODE_MAX_COUNTER}"):
        rolling_code(TEST_SERIAL, -1)


@pytest.mark.parametrize(("counter", "expected"), ROLLING_VECTORS)
def test_invert_rolling_code_recovers_serial_and_counter(counter: int, expected: str) -> None:
    frame = RadioFrame.from_hex(expected)
    assert invert_rolling_code(frame.rolling_code) == (TEST_SERIAL, counter)


def test_invert_rolling_code_round_trips_any_serial() -> None:
    for serial in (0, 1, 0x800000, 0xFFFFFF, 0x123456):
        for counter in (0, 5, 512, ROLLING_CODE_MAX_COUNTER):
            assert invert_rolling_code(rolling_code(serial, counter)) == (serial, counter)


def test_invert_rolling_code_refuses_unknown_counter_bits() -> None:
    code = bytearray(rolling_code(TEST_SERIAL, 3))
    code[1] |= 0x02  # frame bit 14
    with pytest.raises(ValueError, match=r"\[14\]"):
        invert_rolling_code(bytes(code))
    with pytest.raises(ValueError, match="5 bytes"):
        invert_rolling_code(b"\x00")


def test_seed_xor_from_recovers_the_constant() -> None:
    for counter in (0, 1, 300, 1023):
        assert seed_xor_from(TEST_SERIAL, rolling_code(TEST_SERIAL, counter)) == TEST_SEED_XOR
    with pytest.raises(ValueError, match="24-bit"):
        seed_xor_from(1 << 24, rolling_code(TEST_SERIAL, 0))


def test_seed_xor_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    assert seed_xor() == TEST_SEED_XOR
    monkeypatch.setenv(SEED_XOR_ENV, "0x123456")
    assert seed_xor() == 0x123456
    assert rolling_code(TEST_SERIAL, 5) != RadioFrame.from_hex(ROLLING_VECTORS[0][1]).rolling_code
    monkeypatch.setenv(SEED_XOR_ENV, "0x1000000")
    with pytest.raises(ValueError, match="24-bit"):
        seed_xor()
    monkeypatch.delenv(SEED_XOR_ENV)
    with pytest.raises(SeedXorMissingError, match="calibrate"):
        rolling_code(TEST_SERIAL, 0)
