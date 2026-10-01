"""Advance a device's radio counter on the box, one real transmission at a time.

Reading a higher counter bit on air means making a transmitter cross 512, 1024, 2048…
The box moves a device's counter by exactly one per accepted request, and collapses the
repeated commands of one request into a single transmission, so the only way up is one
request per step. Use a command that changes nothing (``STOP`` on an idle cover, a light
already in that state, or any command of a device no receiver has learned).

Each step is a ~1.6 s transmission: mind the 868 MHz duty-cycle limit (1 % in most of
the band in the EU, i.e. about 36 s of transmission per hour).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from aioteleco import TelecoHub
from aioteleco.devices import Device
from aioteleco.exceptions import TelecoError

from .memory import radio_counter

_LOGGER = logging.getLogger(__name__)


class CounterStalledError(RuntimeError):
    """The counter stopped moving although the box accepted the sends."""


async def drive_counter(
    hub: TelecoHub,
    device: Device,
    target: int,
    action: str,
    param: str,
    *,
    pace: float = 3.5,
    check_every: int = 10,
    max_stalls: int = 3,
    on_progress: Callable[[int, int], None] | None = None,
) -> int:
    """Send ``(action, param)`` to ``device`` until its counter reaches ``target``.

    The counter is read back every ``check_every`` sends, and the batch before each check
    is cut so that the target is not overshot. ``on_progress(sends, counter)`` is called
    after each check. Returns the final counter.
    """
    counter = await radio_counter(hub, device)
    sends = stalls = 0
    while counter < target:
        batch = min(check_every, target - counter)
        done = 0
        while done < batch:
            try:
                await device.send(action, param)
            except TelecoError as err:  # e.g. the box dropped the LAN connection: nothing sent
                _LOGGER.warning("send failed, retrying: %s", err)
                await asyncio.sleep(2)
                continue
            done += 1
            sends += 1
            await asyncio.sleep(pace)
        new = await radio_counter(hub, device)
        if on_progress:
            on_progress(sends, new)
        stalls = stalls + 1 if new == counter else 0
        if stalls >= max_stalls:
            raise CounterStalledError(f"counter stuck at {new} after {sends} sends")
        counter = new
    return counter
