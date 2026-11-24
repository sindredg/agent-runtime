"""A wait keeps the provider's error text, cut to a fixed length (issue 128).

Each wait is a step in an append-only log, and a job can wait thousands of
times. A 200 kB error body once became a 200,112-character step.
"""

import random

from runtime.provider.base import ProviderError
from runtime.store.steps import StatusCause
from runtime.worker.agent import _CUT, ERROR_TEXT_LENGTH, _ProviderBusy, _ProviderFault, _wait

# The wait's jitter, fixed so the run repeats.
RNG_SEED = 1


def rng() -> random.Random:
    return random.Random(RNG_SEED)  # noqa: S311 - jitter, not a secret


def busy(message: str) -> _ProviderBusy:
    error = ProviderError(message, status=429, retryable=True, busy=True, retry_after=5.0)
    return _ProviderBusy(error, attempt=3)


def test_a_long_error_text_is_cut_to_the_fixed_length() -> None:
    wait = _wait(busy("provider answered 429: " + "x" * 200_000), rng())
    assert len(wait.error) == ERROR_TEXT_LENGTH
    assert wait.error.startswith("provider answered 429: xxx")
    assert wait.error.endswith(_CUT)


def test_a_short_error_text_is_kept_whole() -> None:
    wait = _wait(busy("provider answered 429: slow down"), rng())
    assert wait.error == "provider answered 429: slow down"
    assert (wait.attempt, wait.retry_after) == (3, 5.0)
    assert wait.seconds >= 5.0


def test_a_text_of_exactly_the_length_is_kept_whole() -> None:
    text = "e" * ERROR_TEXT_LENGTH
    assert _wait(busy(text), rng()).error == text


def test_a_wait_names_its_cause() -> None:
    assert _wait(busy("slow down"), rng()).cause is StatusCause.PROVIDER_BUSY
    failed = ProviderError("provider answered 500: oops", status=500, retryable=True)
    assert _wait(_ProviderFault(failed, attempt=1), rng()).cause is StatusCause.PROVIDER_ERROR
