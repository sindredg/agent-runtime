"""How long a job waits for the provider, and how often (ADR 0018, ADR 0030). Pure logic.

The wait grows with each wait since the last response, of either cause:
`d = min(CAP, BASE * 2**(n-1))` for the n-th. It is at least `d/2` and at
least the provider's `retry-after`, plus a random part of up to `d/2`. The
floor makes the number of tries in a window bounded; the random part keeps
jobs that were refused together from coming back together.

A provider error that may pass is waited out up to two caps. A busy answer
has no cap.
"""

import random

BASE_SECONDS = 2.0
CAP_SECONDS = 60.0
# Waits for a provider error since the last response. Ten last 181 to 362 s
# without a `retry-after`, which rides out an incident of a few minutes.
MAX_ERRORS_IN_A_ROW = 10
# Waits for a provider error over the job's life. A failed call can be billed
# and is not in the budget, so this bounds such calls: 30, the one that fails,
# and one for each worker lost before its wait was stored.
MAX_ERRORS_IN_JOB = 30


def ceiling(attempt: int) -> float:
    """`d` for the `attempt`-th wait since the last response, counted from 1."""
    if attempt < 1:
        raise ValueError("attempt counts from 1")
    # 2**64 is far past the cap. A larger exponent would only cost time.
    return min(CAP_SECONDS, BASE_SECONDS * 2.0 ** min(attempt - 1, 64))


def floor(attempt: int, retry_after: float | None) -> float:
    """The shortest wait for the `attempt`-th wait since the last response."""
    return max(retry_after or 0.0, ceiling(attempt) / 2)


def delay(attempt: int, retry_after: float | None, rng: random.Random) -> float:
    """Seconds to wait before the job is sent again: the floor plus up to `d/2`."""
    return floor(attempt, retry_after) + rng.uniform(0, ceiling(attempt) / 2)


def error_cap(in_a_row: int, in_job: int) -> str | None:
    """The cap a further provider-error wait would pass, as words. None if it may wait.

    The counts are the waits for a provider error already in the job's log.
    """
    if in_a_row >= MAX_ERRORS_IN_A_ROW:
        return f"gave up at the cap of {MAX_ERRORS_IN_A_ROW} provider errors in a row"
    if in_job >= MAX_ERRORS_IN_JOB:
        return f"gave up at the cap of {MAX_ERRORS_IN_JOB} provider errors in the job"
    return None
