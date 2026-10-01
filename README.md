# radio-teleco

Reverse engineering of the **868 MHz radio link** between Teleco Automation boxes (Daisy,
and the other brand apps) and their receivers (motors, lights), with a Python library and a
CLI to work with it.

Driving a box from the cloud or the LAN is the job of
[aioteleco](https://github.com/trois-six/aioteleco). This project is about the radio the box
emits: it is the groundwork for an open-source replacement of the box, or for a
transmitter that talks to the receivers directly.

## Status

Fully reverse-engineered for every case seen so far (details in
[docs/radio.md](docs/radio.md)):

* physical layer: 868.30 MHz 2-FSK, 515 µs pulse-width coded segments;
* frame: 64 bits, a 5-byte rolling code, a channel nibble and an additive checksum;
* rolling code: `F(serial, counter)` solved for counters 0..1023, byte-exact on every
  captured frame and on live predictions.

Open: counter bits 10..15 (never seen on air), and the serial-to-seed permutation for
serials from outside the box's own block.

## Install

```sh
pip install 'radio-teleco[cli,sdr]'   # or: uv tool install 'radio-teleco[cli,sdr]'
```

`cli` adds the `radio-teleco` command; `sdr` adds numpy for decoding captures.

## Library

```python
from radio_teleco import RadioFrame, invert_rolling_code  # needs RADIO_TELECO_SEED_XOR

frame = RadioFrame.from_serial(0xABCDEF, counter=42, channel=7)
print(frame, frame.valid)                         # 8 bytes, checksum OK
print(invert_rolling_code(frame.rolling_code))   # (0xABCDEF, 42)
```

`radio_teleco.memory` reads the box's transmitter serials and per-device counters through
an aioteleco `TelecoHub` (read-only `MEMORY` command); `radio_teleco.drive` advances a
device's counter; `radio_teleco.sdr` decodes `rtl_sdr` captures.

## CLI

```sh
radio-teleco decode "<8 hex bytes>"             # channel, checksum, serial, counter
radio-teleco frame 0xABCDEF 42 7                 # build a frame
radio-teleco calibrate <serial> "<8 hex bytes>"  # recover the seed constant
radio-teleco serials                             # the box's transmitter serials
radio-teleco counter "LED"                       # a device's last counter
radio-teleco drive "Test" 1024 POWER OFF         # advance a counter with a no-op command

rtl_sdr -f 868000000 -s 2400000 -g 40 captures/run.cu8
radio-teleco bursts captures/run.cu8             # where the bursts are
radio-teleco demod captures/run.cu8 --start 10 --end 20
```

## Configuration

The rolling code's **seed constant is not in the repository**: it may depend on the box's
block of transmitter serials (see [Open questions](docs/radio.md#open-questions)), so it is
treated like personal data. Copy `.env.example` to `.env` (git-ignored) and set
`RADIO_TELECO_SEED_XOR`. To recover it, read your box's serials (`radio-teleco serials`),
capture one frame of one of those devices, and run
`radio-teleco calibrate <serial> "<frame>"`.

Box credentials come from `--email/--password`, `TELECO_EMAIL` / `TELECO_PASSWORD` /
`TELECO_LOCAL_IP` (the same `.env` works), or `~/.config/aioteleco/config.toml`, as for
aioteleco's `teleco`. Run with `uv run --env-file .env radio-teleco …`, or export the
variables.

## Safety and privacy

* Only send commands that change nothing (`STOP` on an idle cover, a light already in that
  state, or a device no receiver has learned). Each one is a real ~1.6 s transmission:
  mind the 868 MHz duty-cycle limit.
* Captures and decoded frames carry real transmitter serials and counters, which is what a
  replay or a clone needs. Keep them out of the repository (`captures/` and `*.cu8` are
  git-ignored) and out of issues, as well as the seed constant (`.env`). Tests use a
  throwaway serial (`0xABCDEF`) and a throwaway seed constant.
* The RTL-SDR only receives. Transmitting on 868 MHz is regulated: check your local rules.

## Development

```sh
uv sync
uv run pytest
uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy
```

## License

MIT
