"""Generate UUIDv7 run keys without relying on ``uuid.uuid7``.

This environment's stdlib ships a stripped-down ``uuid`` module with no
``uuid7`` helper, so ATE needs its own generator to honour the "UUIDv7 run key"
contract (spec rule 94). The layout follows RFC 9562: a 48-bit millisecond
timestamp in the first six bytes, version nibble 7 on byte 6 (which lands at
string index 14 of the canonical form), and variant bits on byte 8.

It is intentionally self-contained — no dependency on the stdlib function — so it
works wherever ATE runs. Output is uppercase: run keys are canonical uppercase hex,
so byte 6's version nibble reads as "7" rather than "d".
"""

from __future__ import annotations

import os
import time
import uuid as _uuid


def new_uuid7() -> str:
    """Return a fresh UUIDv7 string (canonical, hyphenated, uppercase)."""
    ts_ms = time.time_ns() // 1_000_000

    b = bytearray(os.urandom(16))   # fully random base; override only the fixed fields
    # Millisecond timestamp across the first six bytes (48 bits, big-endian).
    for i in range(6):
        b[i] = (ts_ms >> (48 - 8 * i)) & 0xFF
    # Version: high nibble of byte 6 is 7 -> string index 14. The low two bits keep
    # the millisecond's remainder, as RFC 9562 specifies.
    b[6] = (b[6] & 0x0F) | (7 << 4)
    # Variant: top two bits of byte 8 are 10.
    b[8] = (b[8] & 0x3F) | (0b10 << 6)

    return str(_uuid.UUID(bytes=bytes(b))).upper()


if __name__ == "__main__":
    key = new_uuid7()
    assert len(key) == 36 and key[14] == "7", f"not a UUIDv7: {key}"
    print(key)
