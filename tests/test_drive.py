"""Driving a device's counter up."""

from __future__ import annotations

import pytest

from radio_teleco.drive import CounterStalledError, drive_counter

from .conftest import FakeBox


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instant(_: float) -> None:
        return None

    monkeypatch.setattr("radio_teleco.drive.asyncio.sleep", instant)


async def test_drive_reaches_the_target_without_overshooting(box: FakeBox) -> None:
    box.counters[7] = 100
    progress: list[tuple[int, int]] = []
    final = await drive_counter(
        box.hub(),
        box.device(7),
        125,
        "POWER",
        "OFF",
        pace=0,
        on_progress=lambda s, c: progress.append((s, c)),
    )
    assert final == box.counters[7] == 125
    assert len(box.sends) == 25
    assert set(box.sends) == {("POWER", "OFF")}
    assert progress == [(10, 110), (20, 120), (25, 125)]


async def test_drive_retries_refused_sends(box: FakeBox) -> None:
    box.counters[2], box.fail_sends = 10, 2
    assert await drive_counter(box.hub(), box.device(2), 12, "STOP", "STOP", pace=0) == 12
    assert len(box.sends) == 2


async def test_drive_stops_when_the_counter_is_stuck(box: FakeBox) -> None:
    box.counters[2], box.stuck = 10, True
    with pytest.raises(CounterStalledError, match="stuck at 10"):
        await drive_counter(box.hub(), box.device(2), 100, "STOP", "STOP", pace=0, check_every=1)
    assert len(box.sends) == 3


async def test_drive_does_nothing_at_the_target(box: FakeBox) -> None:
    box.counters[2] = 50
    assert await drive_counter(box.hub(), box.device(2), 40, "STOP", "STOP") == 50
    assert box.sends == []
