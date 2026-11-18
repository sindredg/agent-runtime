"""The wait after a busy provider (ADR 0018): its floor, its ceiling, `retry-after`."""

import random

import pytest
from hypothesis import given
from hypothesis import strategies as st

from runtime.worker.backoff import CAP_SECONDS, ceiling, delay, floor


def test_the_ceiling_doubles_from_two_seconds_up_to_the_cap() -> None:
    assert [ceiling(n) for n in range(1, 9)] == [2, 4, 8, 16, 32, 60, 60, 60]
    assert ceiling(10_000) == CAP_SECONDS


def test_attempts_count_from_one() -> None:
    with pytest.raises(ValueError, match="from 1"):
        ceiling(0)


def test_retry_after_raises_the_floor_and_never_lowers_it() -> None:
    assert [floor(n, None) for n in range(1, 7)] == [1, 2, 4, 8, 16, 30]
    assert [floor(n, 5) for n in range(1, 7)] == [5, 5, 5, 8, 16, 30]
    assert floor(1, 0) == 1


def test_the_pass_lines_bound_six_busy_answers_in_sixty_seconds() -> None:
    """With `retry-after: 5`, the seventh request comes 69 s after the first or later.

    So a window of 60 s that starts before the first request refuses at most six.
    """
    starts = [0.0]
    for attempt in range(1, 7):
        starts.append(starts[-1] + floor(attempt, 5))
    assert starts == [0, 5, 10, 15, 23, 39, 69]
    assert sum(1 for start in starts if start < 60) == 6


@given(
    attempt=st.integers(min_value=1, max_value=200),
    retry_after=st.none() | st.floats(min_value=0, max_value=86_400),
    seed=st.integers(),
)
def test_a_delay_lies_between_its_floor_and_half_a_ceiling_above_it(
    attempt: int, retry_after: float | None, seed: int
) -> None:
    waited = delay(attempt, retry_after, random.Random(seed))  # noqa: S311 - jitter
    assert waited >= (retry_after or 0)
    assert (
        floor(attempt, retry_after) <= waited <= floor(attempt, retry_after) + ceiling(attempt) / 2
    )


def test_the_random_part_spreads_jobs_that_were_refused_together() -> None:
    rng = random.Random(7)  # noqa: S311 - jitter, not a secret
    delays = {round(delay(3, 5, rng), 6) for _ in range(20)}
    assert len(delays) == 20
    assert all(5 <= value <= 9 for value in delays)
