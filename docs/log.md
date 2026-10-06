# Directory Update Log

## 2026-10-05
* **Update**: Documented the LilyGO SX1276 transmitter's [hardware validation](../firmware/README.md#hardware-validation-2026-10-05): 12/12 byte-exact frames, measured FSK tones and PWM timings, no observed ADC clipping at 8.7 dB gain, and local capture evidence. Clarified that the burst detector reports a spectral peak, not the carrier midpoint. Recorded serial-open resets and volatile cooldown precautions. Real Teleco receiver acceptance and the existing rolling-code open questions remain untested.

## 2026-10-01
* **Creation**: Moved the [Radio link](radio.md) notes here from aioteleco, without the value of the rolling code's seed constant (it may depend on the box's serial block), and recorded that unused device slots start at counter 1 and that rooms are cloud-side only.

## 2026-09-30
* **Update**: Counter bit 9 read on air on two serials: the rolling code is solved for counters 0..1023.
* **Creation**: Physical layer, frame coding, frame fields and rolling-code algorithm reverse-engineered from on-air captures.
