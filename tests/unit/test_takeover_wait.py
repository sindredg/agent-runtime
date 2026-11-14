"""The wait before a decision about an interrupted sandboxed call: what ends it, and how."""

import asyncio
import time
import uuid
from datetime import timedelta

import pytest

from runtime.sandbox.runner import LONGEST_BOUND
from runtime.store.held import LeaseLost
from runtime.worker.agent import _bound, _wait_unless_stopped
from runtime.worker.lease import Lease

JOB = uuid.UUID("01a1d655-88bd-7468-afc4-5ca3a1947906")


def lease() -> Lease:
    return Lease(JOB, 2, timedelta(seconds=30), claimed_at=time.monotonic())


def test_the_wait_runs_its_full_length() -> None:
    async def go() -> tuple[bool, float]:
        started = time.monotonic()
        done = await _wait_unless_stopped(0.2, asyncio.Event(), lease())
        return done, time.monotonic() - started

    done, took = asyncio.run(go())

    assert done is True
    assert took >= 0.2


def test_a_stop_ends_the_wait_without_a_decision() -> None:
    async def go() -> tuple[bool, float]:
        stop = asyncio.Event()
        asyncio.get_running_loop().call_later(0.05, stop.set)
        started = time.monotonic()
        done = await _wait_unless_stopped(30, stop, lease())
        return done, time.monotonic() - started

    done, took = asyncio.run(go())

    assert done is False
    assert took < 5


def test_a_stop_that_came_first_ends_the_wait_at_once() -> None:
    async def go() -> bool:
        stop = asyncio.Event()
        stop.set()
        return await _wait_unless_stopped(30, stop, lease())

    assert asyncio.run(go()) is False


def test_a_lost_lease_ends_the_wait_with_lease_lost() -> None:
    async def go() -> None:
        held = lease()
        asyncio.get_running_loop().call_later(0.05, held.mark_lost)
        await _wait_unless_stopped(30, asyncio.Event(), held)

    with pytest.raises(LeaseLost):
        asyncio.run(go())


def test_a_broken_watcher_is_raised_and_not_read_as_the_end_of_the_wait() -> None:
    class Broken(Lease):
        async def wait_lost(self) -> None:
            raise RuntimeError("watcher broke")

    async def go() -> None:
        held = Broken(JOB, 2, timedelta(seconds=30), claimed_at=time.monotonic())
        await _wait_unless_stopped(30, asyncio.Event(), held)

    with pytest.raises(RuntimeError, match="watcher broke"):
        asyncio.run(go())


@pytest.mark.parametrize(
    ("recorded", "bound"),
    [(57.0, 57.0), (0, 0.0), (1e300, LONGEST_BOUND), (-1, None), ("57", None), (True, None)],
)
def test_the_bound_comes_from_the_intent_and_is_never_unbounded(
    recorded: object, bound: float | None
) -> None:
    """A tool the worker does not have: only the intent's number counts."""
    assert _bound(None, {"ends_within": recorded}) == bound
