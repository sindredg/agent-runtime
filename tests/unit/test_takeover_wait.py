"""The wait before a decision about an interrupted call: its length, what ends it, and how."""

import asyncio
import math
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from runtime.sandbox.runner import LONGEST_BOUND
from runtime.store.held import LeaseLost
from runtime.tools.base import LONGEST_TOOL_BOUND, Tool, ToolResult
from runtime.worker import agent
from runtime.worker.agent import _bound, _outlast_container, _wait_out, _wait_unless_stopped
from runtime.worker.halt import Halt
from runtime.worker.lease import Lease

JOB = uuid.UUID("01a1d655-88bd-7468-afc4-5ca3a1947906")


def lease() -> Lease:
    return Lease(JOB, 2, timedelta(seconds=30), claimed_at=time.monotonic())


def test_the_wait_runs_its_full_length() -> None:
    async def go() -> tuple[bool, float]:
        started = time.monotonic()
        done = await _wait_unless_stopped(0.2, asyncio.Event(), lease(), Halt(JOB))
        return done, time.monotonic() - started

    done, took = asyncio.run(go())

    assert done is True
    assert took >= 0.2


def answers(*values: float) -> tuple[Callable[[], Awaitable[float]], list[int]]:
    """A stand-in for the database: one answer per ask, and the number of asks."""
    asked = [0]

    async def left_now() -> float:
        asked[0] += 1
        return values[asked[0] - 1]

    return left_now, asked


def test_a_wait_that_ends_early_by_the_clock_that_counts_waits_out_the_rest() -> None:
    # The worker sleeps on its own clock. When the clock that counts ran
    # faster meanwhile, it asks again and waits out what is left.
    left_now, asked = answers(0.1, 0.15, -0.01)

    async def go() -> tuple[bool, float]:
        started = time.monotonic()
        done = await _wait_out(left_now, asyncio.Event(), lease(), Halt(JOB), JOB)
        return done, time.monotonic() - started

    done, took = asyncio.run(go())

    assert done is True
    assert asked == [3]
    assert took >= 0.25


def test_nothing_is_waited_when_the_bound_has_passed() -> None:
    left_now, asked = answers(0.0)
    assert asyncio.run(_wait_out(left_now, asyncio.Event(), lease(), Halt(JOB), JOB)) is True
    assert asked == [1]


def test_a_stop_during_the_second_wait_ends_it_without_a_decision() -> None:
    async def go() -> bool:
        stop = asyncio.Event()
        left_now, _ = answers(0.01, 30.0)
        asyncio.get_running_loop().call_later(0.1, stop.set)
        async with asyncio.timeout(5):
            return await _wait_out(left_now, stop, lease(), Halt(JOB), JOB)

    assert asyncio.run(go()) is False


@pytest.mark.parametrize("first", [True, False], ids=["first ask", "second ask"])
def test_a_stop_during_an_ask_ends_the_wait_without_a_decision(first: bool) -> None:
    async def go() -> bool:
        stop = asyncio.Event()
        asked = 0

        async def left_now() -> float:
            nonlocal asked
            asked += 1
            if first or asked == 2:
                asyncio.get_running_loop().call_later(0.05, stop.set)
                await asyncio.Event().wait()  # a database that never answers
            return 0.01

        async with asyncio.timeout(5):
            return await _wait_out(left_now, stop, lease(), Halt(JOB), JOB)

    assert asyncio.run(go()) is False


def outlast(monkeypatch: pytest.MonkeyPatch, answer: Callable[[int], float]) -> int:
    """Run the takeover wait with the database's answers stubbed. Returns how often it asked."""
    asked = 0

    async def seconds_until(engine: Any, moment: datetime) -> float:
        nonlocal asked
        asked += 1
        return answer(asked)

    monkeypatch.setattr(agent, "seconds_until", seconds_until)

    async def go() -> bool:
        return await _outlast_container(
            SimpleNamespace(engine=None),  # type: ignore[arg-type]
            None,
            SimpleNamespace(id=JOB),  # type: ignore[arg-type]
            lease(),
            {"ends_within": 1.0},
            datetime.now(UTC),
            asyncio.Event(),
            Halt(JOB),
        )

    assert asyncio.run(go()) is True
    return asked


def test_the_takeover_wait_asks_the_database_again_until_no_time_is_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert outlast(monkeypatch, lambda asked: [0.05, 0.05, -0.01][asked - 1]) == 3


def test_a_database_error_on_the_second_ask_is_a_store_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def answer(asked: int) -> float:
        if asked == 2:
            raise OSError("connection reset")
        return 0.01

    with pytest.raises(agent._StoreUnreachable):
        outlast(monkeypatch, answer)


def test_a_stop_ends_the_wait_without_a_decision() -> None:
    async def go() -> tuple[bool, float]:
        stop = asyncio.Event()
        asyncio.get_running_loop().call_later(0.05, stop.set)
        started = time.monotonic()
        done = await _wait_unless_stopped(30, stop, lease(), Halt(JOB))
        return done, time.monotonic() - started

    done, took = asyncio.run(go())

    assert done is False
    assert took < 5


def test_a_stop_that_came_first_ends_the_wait_at_once() -> None:
    async def go() -> bool:
        stop = asyncio.Event()
        stop.set()
        return await _wait_unless_stopped(30, stop, lease(), Halt(JOB))

    assert asyncio.run(go()) is False


def test_a_lost_lease_ends_the_wait_with_lease_lost() -> None:
    async def go() -> None:
        held = lease()
        asyncio.get_running_loop().call_later(0.05, held.mark_lost)
        await _wait_unless_stopped(30, asyncio.Event(), held, Halt(JOB))

    with pytest.raises(LeaseLost):
        asyncio.run(go())


def test_a_broken_watcher_is_raised_and_not_read_as_the_end_of_the_wait() -> None:
    class Broken(Lease):
        async def wait_lost(self) -> None:
            raise RuntimeError("watcher broke")

    async def go() -> None:
        held = Broken(JOB, 2, timedelta(seconds=30), claimed_at=time.monotonic())
        await _wait_unless_stopped(30, asyncio.Event(), held, Halt(JOB))

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


def in_worker(ends_within: float | None) -> Tool:
    """A tool that runs in the worker, with the bound it names for its calls."""
    return Tool("act", "Do it.", {}, lambda call: ToolResult("done"), ends_within=ends_within)


def test_a_tool_in_the_worker_is_waited_for_as_long_as_it_says() -> None:
    assert _bound(in_worker(12.0), {}) == 12.0
    # The intent's number comes first: it is what the worker that started the call wrote.
    assert _bound(in_worker(12.0), {"ends_within": 3.0}) == 3.0
    # A tool that names no bound is not waited for, as before.
    assert _bound(in_worker(None), {}) is None


@pytest.mark.parametrize("bound", [0, -1.0, math.inf, math.nan, LONGEST_TOOL_BOUND + 1])
def test_a_tool_cannot_name_a_bound_that_is_no_length_of_time(bound: float) -> None:
    with pytest.raises(ValueError, match="ends_within must be above 0"):
        in_worker(bound)
    assert in_worker(LONGEST_TOOL_BOUND).ends_within == LONGEST_TOOL_BOUND <= LONGEST_BOUND
