import hashlib
import math
import struct

DIMENSIONS = 64
VERSION = "sha256-counter-v1"


def embed(text: str) -> list[float]:
    """Language-neutral hash vectors. Byte layout is pinned in PROTOCOL.md."""
    encoded = text.encode("utf-8", errors="strict")
    values = []
    for counter in range(DIMENSIONS // 8):
        digest = hashlib.sha256(struct.pack(">I", counter) + encoded).digest()
        values.extend(value / 2147483648.0 for value in struct.unpack(">8i", digest))
    # Explicit left-to-right addition matches other IEEE-754 implementations.
    squared = 0.0
    for value in values:
        squared += value * value
    norm = math.sqrt(squared)
    return [x / norm for x in values]
