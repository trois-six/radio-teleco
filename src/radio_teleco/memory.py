"""The box's radio tables, read from its memory with the ``MEMORY`` system command.

The box answers a ``MEMORY`` read in its ``DIAGNOSTIC`` status item, as space-separated
decimal bytes with a leading space (``" 152 8 50"``), or ``ERROR``. Its memory holds one
radio transmitter per device slot: a table of 24-bit serials at
:data:`RADIO_SERIALS_ADDRESS` and the last transmission counter of each device at
:data:`RADIO_COUNTERS_ADDRESS`. Feed both to :func:`~radio_teleco.frame.rolling_code`.

Everything here is read-only: ``MEMORY`` cannot write.
"""

from __future__ import annotations

import asyncio

from aioteleco import TelecoHub
from aioteleco.const import StatusItemCode
from aioteleco.devices import Device
from aioteleco.exceptions import TelecoCommandError
from aioteleco.models import Installation

MEMORY_READ_MAX = 50  # bytes per read
# Radio transmitter serials: one 24-bit little-endian serial per slot, consecutive values.
RADIO_SERIALS_ADDRESS = 0
RADIO_SERIAL_SLOTS = 50
# Radio transmission counters: the last counter sent for device index ``i``, 16-bit
# big-endian at ``RADIO_COUNTERS_ADDRESS + 2 * i``.
RADIO_COUNTERS_ADDRESS = 158

POLL_INTERVAL = 1.0  # seconds between DIAGNOSTIC polls after a MEMORY read


def parse_memory(value: str, count: int) -> bytes:
    """A ``MEMORY`` answer: space-separated decimal bytes (``ERROR`` when refused)."""
    try:
        data = bytes(int(part) for part in value.split())
    except ValueError:
        raise TelecoCommandError(f"box memory read failed: {value.strip()!r}") from None
    if len(data) != count:
        raise TelecoCommandError(f"box memory read returned {len(data)} bytes, not {count}")
    return data


def parse_radio_serials(data: bytes) -> list[int]:
    """The radio serial table (3 bytes per slot, little-endian)."""
    return [int.from_bytes(data[i : i + 3], "little") for i in range(0, len(data) - 2, 3)]


async def _diagnostic(hub: TelecoHub, installation: Installation) -> str:
    items = await hub.api.device_status(installation, installation.id_installation_device)
    return next((i.status_value for i in items if i.code == StatusItemCode.DIAGNOSTIC), "")


async def read_box_memory(
    hub: TelecoHub, installation: Installation, address: int, count: int, *, max_wait: float = 20.0
) -> bytes:
    """Read ``count`` (1..50) bytes of the box's memory at ``address``.

    The box answers in its ``DIAGNOSTIC`` status item, polled until it changes. Reading
    the same bytes twice in a row leaves it unchanged: the call then returns after
    ``max_wait`` seconds.
    """
    if not 1 <= count <= MEMORY_READ_MAX:
        raise ValueError(f"count must be in 1..{MEMORY_READ_MAX}")
    before = await _diagnostic(hub, installation)
    await hub.read_memory(installation, str(address), f"{count:02d}")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_wait
    value = before
    while value == before and loop.time() < deadline:
        await asyncio.sleep(POLL_INTERVAL)
        value = await _diagnostic(hub, installation)
    return parse_memory(value, count)


async def radio_serials(hub: TelecoHub, installation: Installation) -> list[int]:
    """The box's radio transmitter serials, one per device slot."""
    size = 3 * RADIO_SERIAL_SLOTS
    data = b""
    for offset in range(0, size, 48):  # whole slots per read
        data += await read_box_memory(
            hub, installation, RADIO_SERIALS_ADDRESS + offset, min(48, size - offset)
        )
    return parse_radio_serials(data)


async def radio_counter(hub: TelecoHub, device: Device) -> int:
    """The last radio transmission counter the box sent for ``device``."""
    address = RADIO_COUNTERS_ADDRESS + 2 * device.info.device_index
    data = await read_box_memory(hub, device.installation, address, 2)
    return int.from_bytes(data, "big")
