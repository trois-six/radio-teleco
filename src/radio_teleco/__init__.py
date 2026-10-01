"""Reverse engineering of the 868 MHz radio link of Teleco Automation boxes.

* :mod:`radio_teleco.frame` - the frame model and the rolling code (its seed constant comes
  from the ``RADIO_TELECO_SEED_XOR`` environment variable, see ``.env.example``).
* :mod:`radio_teleco.memory` - the box's radio tables (serials, counters), read-only.
* :mod:`radio_teleco.drive` - advance a device's counter, one transmission at a time.
* :mod:`radio_teleco.sdr` - find and decode bursts in an RTL-SDR capture (``sdr`` extra).
"""

from .frame import (
    FREQUENCY_HZ,
    ROLLING_CODE_MAX_COUNTER,
    RadioFrame,
    SeedXorMissingError,
    frame_checksum,
    invert_rolling_code,
    rolling_code,
    seed_xor,
    seed_xor_from,
)

__all__ = [
    "FREQUENCY_HZ",
    "ROLLING_CODE_MAX_COUNTER",
    "RadioFrame",
    "SeedXorMissingError",
    "frame_checksum",
    "invert_rolling_code",
    "rolling_code",
    "seed_xor",
    "seed_xor_from",
]
