"""Test setup: a throwaway seed constant, and a fake box that answers MEMORY reads in its
DIAGNOSTIC item and counts transmissions."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aioteleco import TelecoHub
from aioteleco.const import StatusItemCode
from aioteleco.devices import Device
from aioteleco.exceptions import TelecoConnectionError
from aioteleco.models import Installation

from radio_teleco.frame import SEED_XOR_ENV

# A throwaway seed constant: the tests never use the real one (it stays in the git-ignored
# .env). Set at import, before the test modules build their frames.
TEST_SEED_XOR = 0x2468AC
os.environ[SEED_XOR_ENV] = f"{TEST_SEED_XOR:#08x}"

BOX_DEVICE_ID = 789
COUNTERS_ADDRESS = 158


@dataclass
class FakeBox:
    """``memory`` maps ``"ADDR: a NB: nnR"`` to the DIAGNOSTIC answer; ``counters`` maps a
    device index to its counter, served at the counter table and bumped by each send."""

    memory: dict[str, str] = field(default_factory=dict)
    counters: dict[int, int] = field(default_factory=dict)
    diagnostic: str = " 9 9"
    reads: list[str] = field(default_factory=list)
    sends: list[tuple[str, str]] = field(default_factory=list)
    fail_sends: int = 0
    stuck: bool = False

    # -- the TelecoHub / CloudApi surface used by radio_teleco --------------------------

    async def read_memory(self, installation: Installation, address: str, count: str) -> None:
        param = f"ADDR: {address} NB: {count}R"
        self.reads.append(param)
        index, rest = divmod(int(address) - COUNTERS_ADDRESS, 2)
        if param in self.memory:
            self.diagnostic = self.memory[param]
        elif not rest and index in self.counters:
            self.diagnostic = "".join(f" {b}" for b in self.counters[index].to_bytes(2, "big"))

    async def device_status(self, installation: Installation, device_id: int) -> list[Any]:
        assert device_id == BOX_DEVICE_ID
        return [SimpleNamespace(code=StatusItemCode.DIAGNOSTIC, status_value=self.diagnostic)]

    @property
    def api(self) -> FakeBox:
        return self

    def hub(self) -> TelecoHub:
        return cast(TelecoHub, self)

    def device(self, key: int | str) -> Device:
        """A device of the box, by index (``TelecoHub.device`` takes a name or an id)."""
        box, index = self, int(key)

        async def send(action: str, param: str) -> None:
            if box.fail_sends:
                box.fail_sends -= 1
                raise TelecoConnectionError("refused")
            box.sends.append((action, param))
            if not box.stuck:
                box.counters[index] += 1
            box.diagnostic = ""  # so that the next read is seen as a fresh answer

        return cast(
            Device,
            SimpleNamespace(
                info=SimpleNamespace(device_index=index), installation=INSTALLATION, send=send
            ),
        )


INSTALLATION = cast(Installation, SimpleNamespace(id_installation_device=BOX_DEVICE_ID))


@pytest.fixture
def box(monkeypatch: pytest.MonkeyPatch) -> FakeBox:
    monkeypatch.setattr("radio_teleco.memory.POLL_INTERVAL", 0)
    return FakeBox()
