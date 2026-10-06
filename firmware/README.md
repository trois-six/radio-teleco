# teleco_tx — 868 MHz 2-FSK test transmitter

A minimal Arduino firmware for a **TTGO/LilyGO LoRa32 "T3 v1.6.1"** (ESP32-PICO-D4 +
Semtech SX1276) that reproduces the Teleco physical layer documented in
[`../docs/radio.md`](../docs/radio.md), so captures made with the project's RTL-SDR tooling
can be checked end to end.

It is **isolated from the Python library** — it adds nothing to `radio_teleco`'s
dependencies — and it **never transmits on boot**: it emits exactly one bounded burst per
explicit USB-serial `TX` command, after validating the frame.

## What it does

SX1276 in **FSK continuous mode** (packet engine bypassed → no preamble, sync word, CRC,
whitening or header). The chip's **DIO2 pin is the data line**; its level selects the tone.
The ESP32 **RMT peripheral** drives DIO2 with hardware timing, so each tone is held for an
exact number of 515 µs units with no scheduler/interrupt jitter:

```
sync   upper tone, 4 units
64 ×   alternating lower/upper (first lower); 1 unit = bit 0, 2 units = bit 1
gap    lower tone, 16 units
```

The 8-byte frame (MSB first) repeats back to back for ~1.6 s, `round(1.609 s / ((84 +
popcount) × 515 µs))` times — the same frame-count/popcount relation described in the docs.
The complete burst is submitted in one RMT write, with no software restart between frames.

## Pinmap (T3 v1.6.1 / Paxcounter `ttgov21new`)

| NSS | SCK | MISO | MOSI | RST | DIO0 | DIO1 | DIO2 (DATA) |
|---|---|---|---|---|---|---|---|
| 18 | 5 | 19 | 27 | 23 | 26 | 33 | **32** |

## Toolchain (reproducible)

- `arduino-cli` 1.5.1
- Board platform `esp32:esp32` **3.3.12** (board manager URL
  `https://espressif.github.io/arduino-esp32/package_esp32_index.json`) — provides the RMT
  Arduino API (`rmtInit`/`rmtWrite`) used here.
- Library `RadioLib` 7.8.1

```sh
# one-time
URL=https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli core update-index --additional-urls "$URL"
arduino-cli core install esp32:esp32@3.3.12 --additional-urls "$URL"
arduino-cli lib install RadioLib@7.8.1

# compile (FQBN confirmed against the installed core)
arduino-cli compile --fqbn esp32:esp32:ttgo-lora32:Revision=TTGO_LoRa32_v21new firmware/teleco_tx
```

## Flash (only after confirming antenna + test conditions)

The Debian system `esptool` (v4.7.0) is broken; flashing uses the same pinned PyPI build as
the backup. **Take/verify the flash backup first** (see `../flash-backup/RESTORE.md`).

```sh
PORT=/dev/serial/by-id/usb-1a86_USB_Single_Serial_<your-adapter>-if00  # ls /dev/serial/by-id/
arduino-cli compile --fqbn esp32:esp32:ttgo-lora32:Revision=TTGO_LoRa32_v21new --output-dir firmware/build firmware/teleco_tx
# flash with the working esptool (adjust offsets to arduino-cli's output if needed)
uvx 'esptool@5.4.0' --port "$PORT" --baud 921600 write-flash \
  0x1000 firmware/build/teleco_tx.ino.bootloader.bin \
  0x8000 firmware/build/teleco_tx.ino.partitions.bin \
  0x10000 firmware/build/teleco_tx.ino.bin
```

(`arduino-cli upload` also works; the explicit esptool form documents the offsets.)

## Serial protocol (115200 baud, newline-terminated)

| Command | Effect |
|---|---|
| `PING` | `OK PONG` |
| `INFO` | print version, radio state, freq/dev/power, pins, duty guard |
| `TX <16hex>` | validate 8-byte frame (length + checksum) and emit one auto-length burst |
| `TXN <16hex> <reps>` | same, with an explicit repeat count (1..40) |
| `POL <0\|1>` | which DIO2 level is the UPPER tone (flip if the SDR shows tones inverted) |
| `PWR <dBm>` | output power on PA_BOOST, 2..17 (default 2 = lowest, SDR-safe) |
| `FREQ <MHz>` | carrier, 863.0..870.0 (default 868.30) |
| `DUTY <sec>` | additional minimum pause after a burst, 0..3600 s (default 5 s); never overrides the calculated 1% cooldown |

`TX` **rejects** a frame whose 8 bytes do not sum to 0 mod 256 (the additive checksum), so
malformed frames are never emitted. Build valid frames with the Python library, e.g.
`radio-teleco frame 0xABCDEF 42 7` or `RadioFrame.from_serial(...)`.
Invalid numeric values, trailing arguments, embedded NULs and overlong lines are rejected.
After an overlong or invalid line, input is discarded through the next newline.

## Host regression tests (no hardware)

```sh
g++ -std=c++17 -Wall -Wextra -Werror -Ifirmware/tests \
  firmware/tests/test_teleco_tx.cpp -o /tmp/radio-teleco-firmware-tests
/tmp/radio-teleco-firmware-tests
```

These compile the actual sketch against simulated radio/RMT/serial APIs. They check
boot behavior, waveform construction, complete bursts, strict parsing, cooldowns,
clock wraparound and injected failures. They do not verify physical wiring, RMT
refill behavior on the ESP32, RF timing, frequencies or receiver acceptance; those
still require hardware measurements.

## Hardware validation (2026-10-05)

**Verified: LilyGO -> RTL-SDR v3 -> project decoder, byte-exact.** Acceptance by a
real Teleco receiver has **not** been tested. The transmitted frame used a throwaway
serial and seed; this was a physical-layer test, not a command for an installed device.

The corrected firmware compiled for the T3 v1.6.1 variant, and the host regression
tests passed (130 frame/polarity cases, 80 repeat-count/polarity cases, parsing,
cooldowns, clock wraparound and simulated faults). The connected board reported
`radio=ready`, `rmt=ready` and the corrected `duty_limit=1%` guard. Fault recovery
was tested with mocks, not by deliberately breaking the physical radio.

### Acquisition and results

One explicit `TXN CCB422B3000007A4 12` command at 2 dBm, 868.300 MHz and `POL 1`.
The SDR captured approximately six seconds at 2.4 MS/s, tuned to 868.000 MHz.
Requested tuner gain was 9.9 dB; the driver selected **8.7 dB**, as recorded in its log.

| Measurement | Result |
|---|---|
| Decoded frame | `CC B4 22 B3 00 00 07 A4`, checksum valid, 12/12 identical complete frames |
| Lower / upper tone | 868.283149 / 868.323064 MHz |
| Midpoint / deviation | 868.303107 MHz / +/-19.957 kHz |
| Strongest FFT peak (`bursts`) | 868.323483 MHz, the upper tone, **not** the carrier midpoint |
| Sync (12 measured) | 2060 us |
| Bit 0 (516 segments) | 513.333..516.667 us; nominal 515 us |
| Bit 1 (252 segments) | 1026.667..1033.333 us; nominal 1030 us |
| Interior gaps (11 measured) | 8240 us; no additional inter-frame delay resolved |
| Sync-to-sync periods | 54073.333..54076.667 us; nominal 54075 us |
| Maximum absolute data-segment timing error | 3.333 us, one analysis sample |
| Programmed waveform / demodulated window | 648.900 / 649.563 ms |
| Power-window burst detector / firmware duration | 651 / 651 ms |
| Raw ADC values during the burst | I and Q both 104..151; no values at 0 or 255, or within two counts of either rail |

`bursts` reports the strongest FFT bin, not the midpoint of the two FSK tones.
The measured midpoint is about +3.1 kHz above the configured frequency; interpreting
the upper-tone peak as the midpoint would incorrectly suggest a +23.5 kHz offset.

The frame contains 21 set bits, so its programmed duration is
`(84 + 21) * 515 us = 54075 us`; twelve repetitions total 648.900 ms. The power
detector uses 1 ms windows, while the demodulator uses filtering and amplitude
thresholds. Their different burst boundaries, and radio startup/shutdown, mean
neither duration should be equated with the exact programmed waveform length.

### Evidence and limits

Local evidence is kept in `captures/2026-10-05-lilygo-validation/`: `test.cu8`,
`metadata.json`, `rtl_sdr.log` and `analysis.json`. The entire directory is ignored
by Git; it is available on this machine, not included in a clone of the repository.
The capture's SHA-256 is
`f1609ef69e1f8167e3b2c2a1ce2409274155831f4119353c765c8f4c8b62c0a4`.

To recheck the saved capture **without transmitting**, from the repository root:

```sh
uv run radio-teleco bursts captures/2026-10-05-lilygo-validation/test.cu8
RADIO_TELECO_SEED_XOR=0x123456 uv run radio-teleco demod captures/2026-10-05-lilygo-validation/test.cu8
```

`0x123456` is the throwaway seed constant for this test only. With this value,
the decoder reports serial `0xABCDEF`, counter 42 and channel 7; it is not the
installation's seed and must not replace the private value in `.env`.

Frequency and timing measurements are relative to an uncalibrated RTL-SDR clock.
Tone durations were measured after filtering and 8x decimation (3.333 us resolution).
The first sync and final gap are subject to burst-edge uncertainty; the gap results
above use only interior gaps. No ADC clipping was observed at 8.7 dB gain, but that
does not rule out analog front-end compression, and a high SNR alone is not proof
of an unsaturated receiver. This validates the tested waveform, not every possible
frame, RF compliance, range or receiver acceptance.

### Resume checklist

- Opening the serial port produced boot/reset messages even with DTR and RTS set
  false before opening. Do not assume reconnecting preserves the volatile cooldown;
  maintain airtime spacing externally across resets. Avoid automatic TX retries.
- Review the saved evidence before considering another RF test; no new flash is
  needed just to reproduce the offline decoding.
- Next milestone: explicitly authorized acceptance testing on the owner's Teleco
  receiver. First determine its learning procedure and a valid dedicated test
  identity; do not assume pairing will make an arbitrary seed valid. Do not reuse
  the box's active identity or advance its counter. No real-actuator test is authorized
  by this validation record.
- Counters >=1024 and the serial-to-seed mapping outside the fitted serial block
  remain open questions; this synthetic transmission does not resolve them.

## Safety

- Defaults to the **lowest** PA_BOOST power (2 dBm) to avoid saturating the RTL-SDR; use
  distance/attenuation on the bench.
- Never auto-transmits; one bounded burst per command (~1.6 s with `TX`; up to
  3.05 s of programmed waveform with `TXN`, at most 40 frames).
- After every attempted transmission, including errors, the pause is at least
  99 times the charged airtime, or `DUTY`, whichever is greater. Airtime includes
  radio startup and shutdown and is conservatively charged at no less than the
  programmed burst duration. `DUTY 0` cannot disable this protection.
- Radio errors disable further transmission and hold the SX1276 in hardware reset.
  RMT failures disable further transmission and attempt a checked radio standby.
  Recovery requires restarting the board; no failure is acknowledged as `OK TX`.
- Cooldowns are volatile: restarting loses airtime history. This is not a regulatory
  compliance guarantee, especially across resets or for other sub-bands selected
  with `FREQ`. **Respecting local frequency, power and duty-cycle rules remains the
  operator's responsibility.**
- Do not drive a real Teleco receiver/actuator without explicit agreement, and do not emit
  frames that impersonate or advance the counter of a device in use.
