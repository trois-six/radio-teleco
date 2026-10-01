# Directory Update Log

## 2026-10-01
* **Creation**: Moved the [Radio link](radio.md) notes here from aioteleco, without the value of the rolling code's seed constant (it may depend on the box's serial block), and recorded that unused device slots start at counter 1 and that rooms are cloud-side only.

## 2026-09-30
* **Update**: Counter bit 9 read on air on two serials: the rolling code is solved for counters 0..1023.
* **Creation**: Physical layer, frame coding, frame fields and rolling-code algorithm reverse-engineered from on-air captures.
