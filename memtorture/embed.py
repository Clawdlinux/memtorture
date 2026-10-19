import hashlib
import math
import random

DIMENSIONS = 64
SEED = 20261019


def embed(text: str, *, seed: int = SEED) -> list[float]:
    """Pinned hash embedder v1. No model downloads or semantic claims."""
    digest = hashlib.sha256(f"{seed}:{text}".encode()).digest()
    generator = random.Random(int.from_bytes(digest, "big"))
    values = [generator.uniform(-1, 1) for _ in range(DIMENSIONS)]
    norm = math.sqrt(sum(x * x for x in values))
    return [x / norm for x in values]
