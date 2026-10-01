"""Find and decode 868 MHz bursts in an RTL-SDR capture (needs the ``sdr`` extra).

A capture is the raw ``cu8`` file of ``rtl_sdr``: interleaved unsigned 8-bit I/Q, e.g.
``rtl_sdr -f 868000000 -s 2400000 -g 40 capture.cu8``. Decoding follows the
:mod:`~radio_teleco.frame` layer: mix the transmitter down to 0 Hz, low-pass, take an FSK
discriminator, re-center it per burst (the tuner drifts), then read the tone segment
lengths (sync, 1 unit = bit 0, 2 units = bit 1, gap).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

try:
    import numpy as np
    import numpy.typing as npt
except ImportError:  # pragma: no cover
    raise ImportError(
        "SDR decoding needs the 'sdr' extra: pip install 'radio-teleco[sdr]'"
    ) from None

from .frame import FRAME_BYTES, FREQUENCY_HZ, UNIT_US, RadioFrame

DEFAULT_CENTER_HZ = 868_000_000.0
DEFAULT_SAMPLE_RATE = 2_400_000.0
# A handheld Teleco remote carries about 30 kHz below the box.
REMOTE_FREQUENCY_HZ = FREQUENCY_HZ - 30_000

_DECIMATION = 8
_FILTER_TAPS = 63
_FILTER_CUTOFF_HZ = 60_000
_MIN_BURST_S = 0.02


@dataclass(frozen=True, slots=True)
class Burst:
    """A stretch of carrier found by :func:`find_bursts`."""

    start_s: float
    duration_s: float
    peak_hz: float
    snr: float


@dataclass(frozen=True, slots=True)
class DecodedBurst:
    """The frames read from one burst by :func:`decode_bursts`, as bit strings."""

    start_s: float
    duration_s: float
    frames: list[str] = field(default_factory=list)

    @property
    def radio_frames(self) -> Counter[RadioFrame]:
        """The complete 64-bit frames, counted (a burst repeats one frame ~30 times)."""
        return Counter(
            RadioFrame(int(bits, 2).to_bytes(FRAME_BYTES, "big"))
            for bits in self.frames
            if len(bits) == FRAME_BYTES * 8
        )

    @property
    def frame(self) -> RadioFrame | None:
        """The burst's frame by majority vote over its repetitions, if any is complete."""
        common = self.radio_frames.most_common(1)
        return common[0][0] if common else None


def _iq(raw: npt.NDArray[np.uint8]) -> npt.NDArray[np.complex64]:
    samples = raw.astype(np.float32) - 127.5
    return (samples[0::2] + 1j * samples[1::2]).astype(np.complex64)


def _open(path: str | Path) -> npt.NDArray[np.uint8]:
    return np.memmap(path, dtype=np.uint8, mode="r")


def find_bursts(
    path: str | Path,
    *,
    center_hz: float = DEFAULT_CENTER_HZ,
    sample_rate: float = DEFAULT_SAMPLE_RATE,
    min_duration_s: float = 0.005,
) -> list[Burst]:
    """Every burst of carrier in the capture, anywhere in the band, with its peak frequency.

    Reads the file in chunks, so long captures fit in memory.
    """
    raw = _open(path)
    total = len(raw) // 2
    window = int(sample_rate / 1000)  # 1 ms power windows
    chunk = int(sample_rate * 10) // window * window
    power = []
    for start in range(0, total, chunk):
        x = _iq(raw[2 * start : 2 * min(start + chunk, total)])
        n = len(x) // window * window
        power.append((np.abs(x[:n]) ** 2).reshape(-1, window).mean(axis=1))
    if not power:
        return []
    p = np.concatenate(power)
    floor = float(np.percentile(p, 10))  # robust even when bursts fill most of the capture
    on = p > 6 * floor
    edges = np.diff(np.concatenate([[0], on.astype(np.int8), [0]]))
    starts, ends = np.where(edges == 1)[0], np.where(edges == -1)[0]
    bursts = []
    for s, e in zip(starts, ends, strict=True):
        if (e - s) / 1000 < min_duration_s:
            continue
        x = _iq(raw[2 * s * window : 2 * min(e * window, total)])[: 1 << 18]
        spectrum = np.abs(np.fft.fftshift(np.fft.fft(x))) ** 2
        freqs = np.fft.fftshift(np.fft.fftfreq(len(x), 1 / sample_rate)) + center_hz
        bursts.append(
            Burst(
                start_s=s / 1000,
                duration_s=(e - s) / 1000,
                peak_hz=float(freqs[np.argmax(spectrum)]),
                snr=float(p[s:e].mean() / floor) if floor else float("inf"),
            )
        )
    return bursts


def _frames(levels: npt.NDArray[np.bool_], durations_us: npt.NDArray[np.float64]) -> list[str]:
    """Bit strings from the (tone, duration) runs of one burst."""
    frames: list[str] = []
    current: str | None = None
    for upper, duration in zip(levels, durations_us, strict=True):
        if upper and 3.3 * UNIT_US < duration < 5 * UNIT_US:  # sync, 4 units
            if current is not None:
                frames.append(current)
            current = ""
        elif current is not None:
            if duration > 5.8 * UNIT_US:  # gap, ~16 units
                frames.append(current)
                current = None
            else:
                current += "1" if duration > 1.5 * UNIT_US else "0"
    return frames


def decode_bursts(
    path: str | Path,
    carrier_hz: float = FREQUENCY_HZ,
    start_s: float = 0.0,
    end_s: float | None = None,
    *,
    center_hz: float = DEFAULT_CENTER_HZ,
    sample_rate: float = DEFAULT_SAMPLE_RATE,
) -> list[DecodedBurst]:
    """Decode the bursts of the transmitter at ``carrier_hz`` between two times.

    The whole window is loaded at once: keep it to a few tens of seconds on long captures
    (use :func:`find_bursts` to locate them).
    """
    raw = _open(path)
    first = int(start_s * sample_rate)
    last = len(raw) // 2 if end_s is None else int(end_s * sample_rate)
    x = _iq(raw[2 * first : 2 * last])
    n = np.arange(len(x))
    x *= np.exp(-2j * np.pi * (carrier_hz - center_hz) * n / sample_rate).astype(np.complex64)
    taps = np.sinc(
        2 * _FILTER_CUTOFF_HZ / sample_rate * (np.arange(_FILTER_TAPS) - _FILTER_TAPS // 2)
    ) * np.hamming(_FILTER_TAPS)
    taps /= taps.sum()
    y = np.convolve(x, taps, "same")[::_DECIMATION]
    rate = sample_rate / _DECIMATION
    amplitude = np.abs(y)
    threshold = max(8 * float(np.percentile(amplitude, 10)), 3.0)
    smooth = int(rate / 2000)  # 0.5 ms
    on = np.convolve(amplitude > threshold, np.ones(smooth), "same") > smooth / 2
    edges = np.diff(on.astype(np.int8))
    starts, ends = np.where(edges == 1)[0], np.where(edges == -1)[0]
    decoded = []
    for s in starts:
        after = ends[ends > s]
        if not len(after) or (after[0] - s) / rate <= _MIN_BURST_S:
            continue
        e = after[0]
        segment = y[s:e]
        tone = np.angle(segment[1:] * np.conj(segment[:-1])) * rate / (2 * np.pi)
        tone -= (np.percentile(tone, 10) + np.percentile(tone, 90)) / 2
        upper = np.convolve(tone > 0, np.ones(9) / 9, "same") > 0.5
        changes = np.concatenate(
            [[0], np.where(np.diff(upper.astype(np.int8)) != 0)[0] + 1, [len(upper)]]
        )
        levels = upper[changes[:-1]]
        durations = np.diff(changes) / rate * 1e6
        decoded.append(
            DecodedBurst(
                start_s=start_s + s / rate,
                duration_s=(e - s) / rate,
                frames=_frames(levels, durations),
            )
        )
    return decoded
