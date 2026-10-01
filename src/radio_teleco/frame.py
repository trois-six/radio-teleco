"""868 MHz radio frames sent by the box to the receivers (motors, lights...).

Observed on air with an SDR receiver: 868.30 MHz, 2-FSK (tones about ±20 kHz around the
carrier). One command is about 1.6 s of carrier repeating the same frame back to back.
A frame is a sync (upper tone, 4 units), 64 segments alternating lower and upper tone
(starting with the lower one) that last one unit (bit 0) or two units (bit 1), then a
lower-tone gap. Bits are read MSB first into 8 bytes:

- bytes 0..4: rolling code, see :func:`rolling_code`. It carries the per-device
  transmission counter (the box keeps one per device, see
  :data:`~radio_teleco.memory.RADIO_COUNTERS_ADDRESS`) mixed with a value derived from the
  transmitter's serial (see :data:`~radio_teleco.memory.RADIO_SERIALS_ADDRESS`).
- byte 5 and the high nibble of byte 6: always 0.
- low nibble of byte 6: the command channel, the ``CHn`` of the device command's
  ``lowlevel_command`` (e.g. a dimmer's ``POWER ON`` is ``CH1``, ``STOP`` is ``CH7``).
- byte 7: checksum, the eight bytes sum to 0 modulo 256.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass

FREQUENCY_HZ = 868_300_000
UNIT_US = 515  # one segment unit
SYNC_UNITS = 4  # upper tone before the first segment
GAP_US = 8120  # lower tone after the last segment
FRAME_BYTES = 8
ROLLING_CODE_BYTES = 5

# -- rolling code (bytes 0..4, bits 0..39) --------------------------------------------
#
# Reverse-engineered from on-air captures correlated with the box's own memory (the
# transmitter serial and the per-device counter, see radio_teleco.memory). Confirmed exact
# against every captured frame and against live predictions for counters not yet seen
# when the model was built. Unknown above ROLLING_CODE_MAX_COUNTER: counter bits 10..15
# have never been observed on air, so their frame bit, rotation amount and XOR key are
# unknown.
#
# The counter's ten low bits sit in the clear at these frame bit positions (bit k of the
# counter -> this frame bit). Bit 9 (frame bit 34) was confirmed by driving a transmitter
# past counter 512 on air, on two different serials.
_COUNTER_BITS = (6, 5, 3, 0, 39, 22, 20, 36, 35, 34)

# 24 other bits of the rolling code are one 24-bit word, scattered into these frame bits
# in this order (cycle position -> frame bit). The remaining 6 (14..17, 28, 29) were 0
# in every capture.
_CYCLE_BITS = (
    1, 2, 4, 7, 8, 9, 24, 25, 10, 26, 11, 27, 12, 13, 30, 31, 32, 33, 18, 19, 21, 37, 38, 23,
)  # fmt: skip
_UNKNOWN_BITS = (14, 15, 16, 17, 28, 29)

# That word starts from a seed derived from the serial (bit p of the seed = bit
# (5 - p) mod 24 of the serial), XORed with a constant, then rotated and XORed once per
# set counter bit (highest counter bit first), and finally XORed with a mask.
#
# The seed was fitted on one box's consecutive serials, so only their low bits varied and
# the permutation is proven on those alone: on the other bits it folds into the seed
# constant. That constant may therefore depend on the box's serial block, so it is kept
# out of the repository and read from the environment (see seed_xor).
SEED_XOR_ENV = "RADIO_TELECO_SEED_XOR"
_ROTATIONS = (1, -1, 1, -1, -1, 1, 1, 1, -1, -1)  # per counter bit 0..9, +left/-right
_STEP_KEYS = (
    0x000000,
    0x75AADB,
    0xAEE77D,
    0x51389A,
    0x081400,
    0xF4D279,
    0x9D6AA2,
    0x31388A,
    0xF56A3E,
    0x563A96,
)
assert len(_COUNTER_BITS) == len(_ROTATIONS) == len(_STEP_KEYS)
ROLLING_CODE_MAX_COUNTER = (1 << len(_STEP_KEYS)) - 1  # 1023, the counters the model covers
_OUTPUT_MASK = 0xDB8DC8
_WORD_BITS = 24
_WORD_MASK = (1 << _WORD_BITS) - 1


def _rotate_left(value: int, amount: int) -> int:
    amount %= _WORD_BITS
    if not amount:
        return value
    return (value << amount | value >> (_WORD_BITS - amount)) & _WORD_MASK


class SeedXorMissingError(RuntimeError):
    """The seed constant is not configured."""


def seed_xor() -> int:
    """The 24-bit seed constant, from the ``RADIO_TELECO_SEED_XOR`` environment variable.

    Recover it once from one captured frame of a transmitter whose serial is known (e.g.
    read with :func:`~radio_teleco.memory.radio_serials`), with :func:`seed_xor_from`.
    """
    value = os.environ.get(SEED_XOR_ENV, "").strip()
    if not value:
        raise SeedXorMissingError(
            f"{SEED_XOR_ENV} is not set: put it in .env (see .env.example), or recover it "
            "with `radio-teleco calibrate <serial> <frame>`"
        )
    constant = int(value, 0)
    if not 0 <= constant <= _WORD_MASK:
        raise ValueError(f"{SEED_XOR_ENV} is a 24-bit value")
    return constant


def _permute(serial: int) -> int:
    return sum(((serial >> ((5 - p) % _WORD_BITS)) & 1) << p for p in range(_WORD_BITS))


def _unpermute(permuted: int) -> int:
    return sum((permuted >> p & 1) << ((5 - p) % _WORD_BITS) for p in range(_WORD_BITS))


def _seed(serial: int) -> int:
    return _permute(serial) ^ seed_xor()


def _scramble(serial: int, counter: int) -> int:
    value = _seed(serial)
    for i in reversed(range(len(_STEP_KEYS))):
        if counter >> i & 1:
            value = _rotate_left(value, _ROTATIONS[i]) ^ _STEP_KEYS[i]
    return value ^ _OUTPUT_MASK


def rolling_code(serial: int, counter: int) -> bytes:
    """The 5-byte rolling code (frame bytes 0..4) for one transmission.

    ``serial`` is the transmitter's 24-bit radio serial (see
    :data:`~radio_teleco.memory.RADIO_SERIALS_ADDRESS`) and ``counter`` is that
    transmitter's transmission number (see :data:`~radio_teleco.memory.RADIO_COUNTERS_ADDRESS`,
    0..:data:`ROLLING_CODE_MAX_COUNTER`).
    """
    if not 0 <= serial < 1 << 24:
        raise ValueError("serial is a 24-bit value (0..16777215)")
    if not 0 <= counter <= ROLLING_CODE_MAX_COUNTER:
        raise ValueError(
            f"counter must be in 0..{ROLLING_CODE_MAX_COUNTER} "
            "(higher counter bits are not reverse-engineered yet)"
        )
    bits = [0] * (ROLLING_CODE_BYTES * 8)
    for k, frame_bit in enumerate(_COUNTER_BITS):
        bits[frame_bit] = counter >> k & 1
    word = _scramble(serial, counter)
    for position, frame_bit in enumerate(_CYCLE_BITS):
        bits[frame_bit] = word >> position & 1
    packed = int("".join(map(str, bits)), 2)
    return packed.to_bytes(ROLLING_CODE_BYTES, "big")


def _unscramble(code: bytes) -> tuple[int, int]:
    """``(seed, counter)`` of a rolling code: the scrambling steps undone."""
    if len(code) != ROLLING_CODE_BYTES:
        raise ValueError(f"the rolling code is {ROLLING_CODE_BYTES} bytes")
    bits = [int(b) for b in f"{int.from_bytes(code, 'big'):0{ROLLING_CODE_BYTES * 8}b}"]
    unknown = [b for b in _UNKNOWN_BITS if bits[b]]
    if unknown:
        raise ValueError(f"frame bits {unknown} set: counter above {ROLLING_CODE_MAX_COUNTER}")
    counter = sum(bits[frame_bit] << k for k, frame_bit in enumerate(_COUNTER_BITS))
    value = sum(bits[frame_bit] << p for p, frame_bit in enumerate(_CYCLE_BITS)) ^ _OUTPUT_MASK
    for i in range(len(_STEP_KEYS)):  # undo the steps, lowest counter bit first
        if counter >> i & 1:
            value = _rotate_left(value ^ _STEP_KEYS[i], -_ROTATIONS[i])
    return value, counter


def invert_rolling_code(code: bytes) -> tuple[int, int]:
    """The ``(serial, counter)`` a rolling code was made from: :func:`rolling_code` run
    backwards, to identify the transmitter of a captured frame.

    Raises :class:`ValueError` when one of the six always-zero frame bits is set: the
    counter is then above :data:`ROLLING_CODE_MAX_COUNTER`, outside the model (and the
    frame is worth keeping, it carries a counter bit never seen yet).
    """
    seed, counter = _unscramble(code)
    return _unpermute(seed ^ seed_xor()), counter


def seed_xor_from(serial: int, code: bytes) -> int:
    """The seed constant (see :func:`seed_xor`), from one rolling code sent by the
    transmitter of a known ``serial``."""
    if not 0 <= serial < 1 << 24:
        raise ValueError("serial is a 24-bit value (0..16777215)")
    seed, _ = _unscramble(code)
    return seed ^ _permute(serial)


def frame_checksum(payload: bytes) -> int:
    """The last byte of a frame: minus the sum of the first seven, modulo 256."""
    return -sum(payload[:7]) & 0xFF


@dataclass(frozen=True, slots=True)
class RadioFrame:
    """One 64-bit frame as sent on air."""

    data: bytes

    def __post_init__(self) -> None:
        if len(self.data) != FRAME_BYTES:
            raise ValueError(f"a radio frame is {FRAME_BYTES} bytes, got {len(self.data)}")

    @classmethod
    def build(cls, rolling_code: bytes, channel: int) -> RadioFrame:
        """Assemble a frame from its rolling code and channel, adding the checksum."""
        if len(rolling_code) != ROLLING_CODE_BYTES:
            raise ValueError(f"the rolling code is {ROLLING_CODE_BYTES} bytes")
        if not 1 <= channel <= 15:
            raise ValueError(f"channel {channel} is not in 1..15")
        payload = rolling_code + bytes((0, channel))
        return cls(payload + bytes((frame_checksum(payload),)))

    @classmethod
    def from_hex(cls, text: str) -> RadioFrame:
        return cls(bytes.fromhex(text))

    @classmethod
    def from_serial(cls, serial: int, counter: int, channel: int) -> RadioFrame:
        """The frame for a transmitter's ``counter``-th transmission (see
        :func:`rolling_code`)."""
        return cls.build(rolling_code(serial, counter), channel)

    @classmethod
    def from_segments(cls, durations_us: Sequence[float], unit_us: float = UNIT_US) -> RadioFrame:
        """Decode the 64 segment durations that follow the sync (gap excluded)."""
        if len(durations_us) != FRAME_BYTES * 8:
            raise ValueError(f"expected {FRAME_BYTES * 8} segments, got {len(durations_us)}")
        bits = 0
        for duration in durations_us:
            units = round(duration / unit_us)
            if units not in (1, 2):
                raise ValueError(f"segment of {duration} us is neither 1 nor 2 units")
            bits = bits << 1 | (units - 1)
        return cls(bits.to_bytes(FRAME_BYTES, "big"))

    def segments_us(self, unit_us: float = UNIT_US) -> list[float]:
        """The 64 segment durations to transmit after the sync (lower tone first)."""
        bits = int.from_bytes(self.data, "big")
        return [unit_us * (1 + (bits >> shift & 1)) for shift in range(FRAME_BYTES * 8 - 1, -1, -1)]

    @property
    def rolling_code(self) -> bytes:
        return self.data[:ROLLING_CODE_BYTES]

    @property
    def channel(self) -> int:
        return self.data[6] & 0x0F

    @property
    def valid(self) -> bool:
        """Checksum right and the always-zero bits (byte 5, high nibble of byte 6) clear."""
        return self.data[7] == frame_checksum(self.data) and self.data[5] == self.data[6] >> 4 == 0

    def __str__(self) -> str:
        return self.data.hex(" ").upper()
