"""Reading the box's radio tables through MEMORY."""

from __future__ import annotations

import pytest
from aioteleco.exceptions import TelecoCommandError

from radio_teleco.memory import (
    parse_memory,
    parse_radio_serials,
    radio_counter,
    radio_serials,
    read_box_memory,
)

from .conftest import INSTALLATION, FakeBox


async def test_read_box_memory(box: FakeBox) -> None:
    box.memory["ADDR: 273 NB: 03R"] = " 1 2 255"
    assert await read_box_memory(box.hub(), INSTALLATION, 273, 3) == b"\x01\x02\xff"
    assert box.reads == ["ADDR: 273 NB: 03R"]
    with pytest.raises(ValueError, match=r"1\.\.50"):
        await read_box_memory(box.hub(), INSTALLATION, 0, 51)


async def test_read_box_memory_errors(box: FakeBox) -> None:
    box.memory.update({"ADDR: 1 NB: 02R": "ERROR", "ADDR: 2 NB: 02R": " 1 2 3"})
    with pytest.raises(TelecoCommandError, match="failed"):
        await read_box_memory(box.hub(), INSTALLATION, 1, 2)
    with pytest.raises(TelecoCommandError, match="3 bytes"):
        await read_box_memory(box.hub(), INSTALLATION, 2, 2)


async def test_read_box_memory_same_answer_waits(box: FakeBox) -> None:
    box.memory["ADDR: 5 NB: 02R"] = " 9 9"  # same as the value already there
    data = await read_box_memory(box.hub(), INSTALLATION, 5, 2, max_wait=0.05)
    assert data == b"\x09\x09"


async def test_radio_counter(box: FakeBox) -> None:
    box.counters[3] = 0x011C
    assert await radio_counter(box.hub(), box.device(3)) == 0x011C
    assert box.reads == ["ADDR: 164 NB: 02R"]


async def test_radio_serials(box: FakeBox) -> None:
    table = b"".join((0x123400 + i).to_bytes(3, "little") for i in range(50))
    box.memory.update(
        {
            f"ADDR: {a} NB: {n:02d}R": "".join(f" {b}" for b in table[a : a + n])
            for a, n in ((0, 48), (48, 48), (96, 48), (144, 6))
        }
    )
    assert await radio_serials(box.hub(), INSTALLATION) == [0x123400 + i for i in range(50)]


def test_parsers() -> None:
    assert parse_memory(" 0 255", 2) == b"\x00\xff"
    assert parse_radio_serials(b"\x01\x02\x03\x04\x05\x06\x07") == [0x030201, 0x060504]
