"""SDR decoding, on synthetic captures built from the frame model."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import numpy.typing as npt
import pytest

from radio_teleco.frame import FREQUENCY_HZ, UNIT_US, RadioFrame
from radio_teleco.sdr import REMOTE_FREQUENCY_HZ, decode_bursts, find_bursts

RATE = 2_400_000.0
CENTER = 868_000_000.0
DEVIATION = 20_000.0
GAP_US = 8120.0
BOX_FRAME = RadioFrame.from_serial(0xABCDEF, 300, 7)
REMOTE_FRAME = RadioFrame.from_serial(0x123456, 513, 8)


def _tones(frame: RadioFrame, repeats: int) -> list[tuple[int, float]]:
    """(tone +1/-1, duration in us) runs of a burst repeating ``frame``."""
    runs: list[tuple[int, float]] = []
    for _ in range(repeats):
        runs.append((1, 4 * UNIT_US))
        runs += [(-1 if i % 2 == 0 else 1, d) for i, d in enumerate(frame.segments_us())]
        runs.append((-1, GAP_US))
    return runs


def _burst(frame: RadioFrame, carrier: float, repeats: int) -> npt.NDArray[np.complex128]:
    freq = np.concatenate(
        [np.full(round(d * 1e-6 * RATE), tone * DEVIATION) for tone, d in _tones(frame, repeats)]
    )
    phase = 2 * np.pi * np.cumsum(freq + carrier - CENTER) / RATE
    return np.asarray(100 * np.exp(1j * phase))


def _capture(path: Path, parts: list[npt.NDArray[np.complex128] | float]) -> None:
    signal = np.concatenate(
        [np.zeros(round(p * RATE), complex) if isinstance(p, float) else p for p in parts]
    )
    rng = np.random.default_rng(0)
    signal += rng.normal(0, 2, len(signal)) + 1j * rng.normal(0, 2, len(signal))
    iq = np.empty(2 * len(signal))
    iq[0::2], iq[1::2] = signal.real, signal.imag
    np.clip(np.round(iq + 127.5), 0, 255).astype(np.uint8).tofile(path)


@pytest.fixture(scope="module")
def capture(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("sdr") / "capture.cu8"
    _capture(
        path,
        [
            0.1,
            _burst(BOX_FRAME, FREQUENCY_HZ, 5),
            0.1,
            _burst(REMOTE_FRAME, REMOTE_FREQUENCY_HZ, 4),
            0.1,
        ],
    )
    return path


def test_find_bursts(capture: Path) -> None:
    box, remote = find_bursts(capture)
    assert box.start_s == pytest.approx(0.1, abs=0.002)
    assert abs(box.peak_hz - FREQUENCY_HZ) < 2 * DEVIATION
    assert abs(remote.peak_hz - REMOTE_FREQUENCY_HZ) < 2 * DEVIATION
    assert box.snr > 100


def test_decode_box_burst(capture: Path) -> None:
    (burst,) = [b for b in decode_bursts(capture, FREQUENCY_HZ, 0.0, 0.5) if b.frames]
    assert burst.frame == BOX_FRAME
    assert burst.radio_frames[BOX_FRAME] >= 4
    assert burst.start_s == pytest.approx(0.1, abs=0.002)


def test_decode_remote_burst_in_a_window(capture: Path) -> None:
    bursts = decode_bursts(capture, REMOTE_FREQUENCY_HZ, start_s=0.45)
    assert [b.frame for b in bursts] == [REMOTE_FRAME]
    assert bursts[0].start_s > 0.45


def test_empty_capture(tmp_path: Path) -> None:
    path = tmp_path / "empty.cu8"
    _capture(path, [0.05])
    assert find_bursts(path) == []
    assert decode_bursts(path) == []
