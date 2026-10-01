"""``radio-teleco`` command line: decode frames and read or drive the box's radio state.

Credentials and the box are configured like the ``teleco`` command of aioteleco:
``--email/--password``, the ``TELECO_EMAIL`` / ``TELECO_PASSWORD`` /
``TELECO_LOCAL_IP`` environment variables, or ``~/.config/aioteleco/config.toml``. The
rolling code's seed constant comes from ``RADIO_TELECO_SEED_XOR``: keep both in the
git-ignored ``.env`` and run ``uv run --env-file .env radio-teleco ...``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tomllib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

try:
    import typer
except ImportError:  # pragma: no cover
    sys.exit("The CLI needs the 'cli' extra: pip install 'radio-teleco[cli]'")

import aiohttp
from aioteleco import TelecoHub
from aioteleco.exceptions import TelecoError
from aioteleco.models import Installation
from aioteleco.transport import TransportMode

from .drive import CounterStalledError, drive_counter
from .frame import (
    FREQUENCY_HZ,
    SEED_XOR_ENV,
    RadioFrame,
    SeedXorMissingError,
    invert_rolling_code,
    seed_xor_from,
)
from .memory import radio_counter, radio_serials, read_box_memory

CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / (
    "aioteleco/config.toml"
)

app = typer.Typer(help="Teleco 868 MHz radio: frames, rolling code, box radio state.")


@dataclass
class Options:
    email: str | None = None
    password: str | None = None
    installation: str | None = None
    transport: TransportMode = TransportMode.AUTO
    local_ip: str | None = None
    as_json: bool = False


OPTS = Options()


@app.callback(no_args_is_help=True)
def main_options(
    email: Annotated[str | None, typer.Option(envvar="TELECO_EMAIL")] = None,
    password: Annotated[str | None, typer.Option(envvar="TELECO_PASSWORD")] = None,
    installation: Annotated[
        str | None, typer.Option("--installation", "-i", help="id, code or name")
    ] = None,
    transport: Annotated[TransportMode, typer.Option(help="how to send commands")] = (
        TransportMode.AUTO
    ),
    local_ip: Annotated[
        str | None, typer.Option(envvar="TELECO_LOCAL_IP", help="box IP on the LAN")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="JSON output")] = False,
    debug: Annotated[bool, typer.Option(help="verbose logs")] = False,
) -> None:
    config: dict[str, Any] = {}
    if CONFIG_PATH.exists():
        config = tomllib.loads(CONFIG_PATH.read_text())
    OPTS.email = email or config.get("email")
    OPTS.password = password or config.get("password")
    OPTS.installation = installation or config.get("installation")
    OPTS.transport = transport
    OPTS.local_ip = local_ip or config.get("local_ip")
    OPTS.as_json = as_json
    logging.basicConfig(level=logging.DEBUG if debug else logging.WARNING)


def _run[T](func: Callable[[TelecoHub, Installation], Awaitable[T]], *, load: bool = True) -> T:
    if not OPTS.email or not OPTS.password:
        typer.echo(
            "Missing credentials (--email/--password, TELECO_EMAIL/TELECO_PASSWORD).", err=True
        )
        raise typer.Exit(2)
    email, password = OPTS.email, OPTS.password

    async def runner() -> T:
        async with aiohttp.ClientSession() as http:
            hub = TelecoHub(
                http, email, password, transport=OPTS.transport, local_host=OPTS.local_ip
            )
            try:
                await hub.connect()
                inst = hub.installation(OPTS.installation)
                if load:
                    await hub.load(inst)
                return await func(hub, inst)
            finally:
                await hub.close()

    try:
        return asyncio.run(runner())
    except TelecoError as err:
        typer.echo(f"Error: {err}", err=True)
        raise typer.Exit(1) from err


def _print(data: Any, text: str) -> None:
    typer.echo(json.dumps(data, indent=2) if OPTS.as_json else text)


def _describe(frame: RadioFrame) -> dict[str, Any]:
    info: dict[str, Any] = {"frame": str(frame), "channel": frame.channel, "valid": frame.valid}
    try:
        serial, counter = invert_rolling_code(frame.rolling_code)
    except ValueError as err:
        info["note"] = str(err)
    else:
        info.update(serial=f"{serial:06X}", counter=counter)
    return info


def _line(info: dict[str, Any]) -> str:
    rest = (
        f"serial {info['serial']} counter {info['counter']}"
        if "serial" in info
        else info.get("note", "")
    )
    return f"{info['frame']}  CH{info['channel']}  valid={info['valid']}  {rest}"


@app.command()
def decode(frames: Annotated[list[str], typer.Argument(help="frames, 8 hex bytes each")]) -> None:
    """Decode frames: channel, checksum, and the transmitter serial and counter."""
    infos = [_describe(RadioFrame.from_hex(frame)) for frame in frames]
    _print(infos, "\n".join(_line(info) for info in infos))


@app.command()
def frame(
    serial: Annotated[str, typer.Argument(help="24-bit serial, e.g. 0xABCDEF")],
    counter: int,
    channel: int,
) -> None:
    """Build the frame a transmitter sends for a counter and a channel."""
    built = RadioFrame.from_serial(int(serial, 0), counter, channel)
    _print({"frame": str(built)}, str(built))


@app.command()
def calibrate(
    serial: Annotated[str, typer.Argument(help="the transmitter's 24-bit serial")],
    frame: Annotated[str, typer.Argument(help="one frame it sent, 8 hex bytes")],
) -> None:
    """Recover the seed constant from one frame of a transmitter whose serial is known.

    Read the serials with `serials`, capture one frame of that device, and put the result
    in .env (git-ignored): it is never committed.
    """
    constant = seed_xor_from(int(serial, 0), RadioFrame.from_hex(frame).rolling_code)
    _print({SEED_XOR_ENV: f"{constant:#08x}"}, f"{SEED_XOR_ENV}={constant:#08x}")


@app.command()
def memory(
    address: int,
    count: Annotated[int, typer.Argument(min=1, max=50)],
) -> None:
    """Read bytes of the box's memory (read-only `MEMORY` command)."""

    async def go(hub: TelecoHub, inst: Installation) -> None:
        data = await read_box_memory(hub, inst, address, count)
        _print({"address": address, "data": data.hex(" ")}, data.hex(" ").upper())

    _run(go, load=False)


@app.command()
def serials() -> None:
    """List the box's radio transmitter serials, one per device slot."""

    async def go(hub: TelecoHub, inst: Installation) -> None:
        table = await radio_serials(hub, inst)
        _print([f"{s:06X}" for s in table], "\n".join(f"{s:06X}" for s in table))

    _run(go, load=False)


@app.command()
def counter(device: str) -> None:
    """Show the last radio transmission counter the box sent for a device."""

    async def go(hub: TelecoHub, _: Installation) -> None:
        value = await radio_counter(hub, hub.device(device))
        _print({"device": device, "counter": value}, str(value))

    _run(go)


@app.command()
def drive(
    device: str,
    target: int,
    action: Annotated[str, typer.Argument(help="a command that changes nothing, e.g. POWER")],
    param: Annotated[str, typer.Argument(help="e.g. OFF on a light that is off")],
    pace: Annotated[float, typer.Option(help="seconds between sends")] = 3.5,
) -> None:
    """Send a no-op command again and again until the device's counter reaches TARGET.

    Every send is a real ~1.6 s transmission: mind the 868 MHz duty cycle.
    """

    def progress(sends: int, value: int) -> None:
        typer.echo(f"{sends} sends, counter {value}", err=True)

    async def go(hub: TelecoHub, _: Installation) -> None:
        try:
            value = await drive_counter(
                hub, hub.device(device), target, action, param, pace=pace, on_progress=progress
            )
        except CounterStalledError as err:
            typer.echo(f"Error: {err}", err=True)
            raise typer.Exit(1) from err
        _print({"device": device, "counter": value}, str(value))

    _run(go)


@app.command()
def bursts(
    capture: Path,
    center: Annotated[float, typer.Option(help="capture center frequency, Hz")] = 868e6,
    rate: Annotated[float, typer.Option(help="capture sample rate, Hz")] = 2.4e6,
) -> None:
    """List the bursts of an rtl_sdr cu8 capture with their peak frequency."""
    from .sdr import find_bursts

    found = find_bursts(capture, center_hz=center, sample_rate=rate)
    _print(
        [
            {"start": b.start_s, "duration": b.duration_s, "peak_hz": b.peak_hz, "snr": b.snr}
            for b in found
        ],
        "\n".join(
            f"t={b.start_s:8.3f}s  {b.duration_s * 1000:5.0f} ms  {b.peak_hz / 1e6:.4f} MHz"
            f"  snr {b.snr:.0f}x"
            for b in found
        ),
    )


@app.command()
def demod(
    capture: Path,
    carrier: Annotated[float, typer.Option(help="transmitter carrier, Hz")] = FREQUENCY_HZ,
    start: Annotated[float, typer.Option(help="window start, s")] = 0.0,
    end: Annotated[float | None, typer.Option(help="window end, s")] = None,
    center: Annotated[float, typer.Option(help="capture center frequency, Hz")] = 868e6,
    rate: Annotated[float, typer.Option(help="capture sample rate, Hz")] = 2.4e6,
) -> None:
    """Decode the frames of the bursts of an rtl_sdr cu8 capture."""
    from .sdr import decode_bursts

    rows = []
    for burst in decode_bursts(capture, carrier, start, end, center_hz=center, sample_rate=rate):
        best = burst.frame
        row: dict[str, Any] = {"start": round(burst.start_s, 3), "repeats": len(burst.frames)}
        if best is not None:
            row.update(_describe(best), votes=burst.radio_frames[best])
        rows.append(row)
    _print(
        rows,
        "\n".join(
            f"t={r['start']:8.3f}s  {r.get('votes', 0)}/{r['repeats']}  "
            + (_line(r) if "frame" in r else "no complete frame")
            for r in rows
        ),
    )


def main() -> None:  # pragma: no cover
    try:
        app()
    except SeedXorMissingError as err:
        sys.exit(f"Error: {err}")
