"""How long a job waits after the provider said it is busy (ADR 0018). Pure logic.

The wait grows with each busy answer in a row: `d = min(CAP, BASE * 2**(n-1))`
for the n-th. It is at least `d/2` and at least the provider's
`retry-after`, plus a random part of up to `d/2`. The floor makes the
number of tries in a window bounded; the random part keeps jobs that were
refused together from coming back together.
"""

import random

BASE_SECONDS = 2.0
CAP_SECONDS = 60.0


def ceiling(attempt: int) -> float:
    """`d` for the `attempt`-th busy answer in a row, counted from 1."""
    if attempt < 1:
        raise ValueError("attempt counts from 1")
    # 2**64 is far past the cap. A larger exponent would only cost time.
    return min(CAP_SECONDS, BASE_SECONDS * 2.0 ** min(attempt - 1, 64))


def floor(attempt: int, retry_after: float | None) -> float:
    """The shortest wait for the `attempt`-th busy answer in a row."""
    return max(retry_after or 0.0, ceiling(attempt) / 2)


def delay(attempt: int, retry_after: float | None, rng: random.Random) -> float:
    """Seconds to wait before the job is sent again: the floor plus up to `d/2`."""
    return floor(attempt, retry_after) + rng.uniform(0, ceiling(attempt) / 2)
