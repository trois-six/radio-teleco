"""The radio-teleco command line."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from radio_teleco import cli
from radio_teleco.frame import SEED_XOR_ENV, RadioFrame, SeedXorMissingError

from .conftest import INSTALLATION, TEST_SEED_XOR, FakeBox
from .test_sdr import BOX_FRAME, _burst, _capture

runner = CliRunner()
FRAME = RadioFrame.from_serial(0xABCDEF, 300, 7)


@pytest.fixture(autouse=True)
def no_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli, "CONFIG_PATH", tmp_path / "missing.toml")
    for var in ("TELECO_EMAIL", "TELECO_PASSWORD", "TELECO_LOCAL_IP"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def online(box: FakeBox, monkeypatch: pytest.MonkeyPatch) -> FakeBox:
    """Run the box commands against the fake box instead of the cloud."""

    def run(func: Callable[[Any, Any], Coroutine[Any, Any, Any]], *, load: bool = True) -> Any:
        return asyncio.run(func(box.hub(), INSTALLATION))

    monkeypatch.setattr(cli, "_run", run)
    monkeypatch.setattr("radio_teleco.drive.asyncio.sleep", _instant)
    return box


async def _instant(_: float) -> None:
    return None


def invoke(*args: str) -> str:
    result = runner.invoke(cli.app, list(args))
    assert result.exit_code == 0, result.output
    return result.output


def test_decode() -> None:
    out = invoke("decode", str(FRAME), "00 00 00 00 00 00 01 00")
    first, second = out.splitlines()
    assert first == f"{FRAME}  CH7  valid=True  serial ABCDEF counter 300"
    assert "valid=False" in second
    (info,) = json.loads(invoke("--json", "decode", str(FRAME)))
    assert (info["serial"], info["counter"], info["channel"]) == ("ABCDEF", 300, 7)


def test_decode_unknown_counter_bits() -> None:
    data = bytearray(FRAME.data)
    data[1] |= 0x02
    out = invoke("decode", RadioFrame.build(bytes(data[:5]), 7).data.hex())
    assert "counter above 1023" in out


def test_frame() -> None:
    assert invoke("frame", "0xABCDEF", "300", "7").strip() == str(FRAME)


def test_calibrate() -> None:
    out = invoke("calibrate", "0xABCDEF", str(FRAME))
    assert out.strip() == f"RADIO_TELECO_SEED_XOR={TEST_SEED_XOR:#08x}"


def test_missing_seed_constant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SEED_XOR_ENV)
    result = runner.invoke(cli.app, ["frame", "1", "2", "3"])
    assert isinstance(result.exception, SeedXorMissingError)


def test_box_commands_need_credentials() -> None:
    result = runner.invoke(cli.app, ["serials"])
    assert result.exit_code == 2
    assert "Missing credentials" in result.output


def test_memory_serials_counter(online: FakeBox) -> None:
    online.memory["ADDR: 273 NB: 02R"] = " 1 255"
    assert invoke("memory", "273", "2").strip() == "01 FF"
    online.memory.update(
        {f"ADDR: {a} NB: {n:02d}R": " 1 0 0" * (n // 3) for a, n in ((0, 48), (96, 48))}
    )
    online.memory.update(
        {f"ADDR: {a} NB: {n:02d}R": " 2 0 0" * (n // 3) for a, n in ((48, 48), (144, 6))}
    )
    serials = json.loads(invoke("--json", "serials"))
    assert serials[:2] == ["000001", "000001"]
    assert serials[-1] == "000002"
    online.counters[4] = 777
    assert invoke("counter", "4").strip() == "777"


def test_drive(online: FakeBox) -> None:
    online.counters[5] = 10
    assert invoke("drive", "5", "13", "POWER", "OFF", "--pace", "0").splitlines()[-1] == "13"
    online.stuck = True
    online.diagnostic = ""  # else re-reading the same counter waits for a fresh answer
    result = runner.invoke(cli.app, ["drive", "5", "50", "POWER", "OFF", "--pace", "0"])
    assert result.exit_code == 1
    assert "stuck at 13" in result.output


def test_bursts_and_demod(tmp_path: Path) -> None:
    path = tmp_path / "c.cu8"
    _capture(path, [0.2, _burst(BOX_FRAME, 868.3e6, 4), 0.2])
    (burst,) = json.loads(invoke("--json", "bursts", str(path)))
    assert burst["start"] == pytest.approx(0.2, abs=0.002)
    assert abs(burst["peak_hz"] - 868.3e6) < 40_000  # one of the two tones
    assert " MHz " in invoke("bursts", str(path))
    (row,) = json.loads(invoke("--json", "demod", str(path)))
    assert row["frame"] == str(BOX_FRAME)
    assert row["votes"] >= 3
    assert "serial ABCDEF counter 300" in invoke("demod", str(path), "--end", "0.6")
