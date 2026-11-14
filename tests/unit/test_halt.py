"""The halt signal, and the race of a piece of work against a halt, a lost lease and a stop."""

import asyncio
import time
import uuid
from datetime import timedelta

import pytest

from runtime.store.held import LeaseLost
from runtime.worker.agent import _race
from runtime.worker.halt import Halt, Halted, HaltReason
from runtime.worker.lease import Lease

JOB = uuid.UUID("01a1d655-88bd-7468-afc4-5ca3a1947906")


def lease() -> Lease:
    return Lease(JOB, 1, timedelta(seconds=30), claimed_at=time.monotonic())


class Work:
    """Records whether it started, and whether it was cancelled."""

    def __init__(self, seconds: float = 30, answer: str = "done") -> None:
        self.seconds = seconds
        self.answer = answer
        self.started = asyncio.Event()
        self.cancelled = False

    async def __call__(self) -> str:
        self.started.set()
        try:
            await asyncio.sleep(self.seconds)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return self.answer


def test_the_first_reason_is_kept() -> None:
    halt = Halt(JOB)
    halt.set(HaltReason.DEADLINE)
    halt.set(HaltReason.CANCELLED)

    with pytest.raises(Halted) as raised:
        halt.check()
    assert raised.value.reason is HaltReason.DEADLINE


def test_work_that_completes_is_returned() -> None:
    work = Work(0)

    async def go() -> tuple[bool, str | None]:
        return await _race(work(), lease=lease(), halt=Halt(JOB))

    assert asyncio.run(go()) == (True, "done")


def test_nothing_is_started_for_a_job_that_is_already_halted() -> None:
    work = Work(0)

    async def go() -> None:
        halt = Halt(JOB)
        halt.set(HaltReason.CANCELLED)
        await _race(work(), lease=lease(), halt=halt)

    with pytest.raises(Halted):
        asyncio.run(go())
    assert not work.started.is_set()


def test_nothing_is_started_under_a_lost_lease() -> None:
    work = Work(0)

    async def go() -> None:
        held = lease()
        held.mark_lost()
        await _race(work(), lease=held, halt=Halt(JOB))

    with pytest.raises(LeaseLost):
        asyncio.run(go())
    assert not work.started.is_set()


@pytest.mark.parametrize("cause", ["halt", "lost", "stop"])
def test_work_is_cancelled_when_something_comes_first(cause: str) -> None:
    work = Work()

    async def go() -> tuple[bool, str | None]:
        halt, held, stop = Halt(JOB), lease(), asyncio.Event()
        loop = asyncio.get_running_loop()
        trigger = {
            "halt": lambda: halt.set(HaltReason.DEADLINE),
            "lost": held.mark_lost,
            "stop": stop.set,
        }[cause]
        loop.call_later(0.05, trigger)
        return await _race(work(), lease=held, halt=halt, stop=stop)

    if cause == "halt":
        with pytest.raises(Halted):
            asyncio.run(go())
    elif cause == "lost":
        with pytest.raises(LeaseLost):
            asyncio.run(go())
    else:
        assert asyncio.run(go()) == (False, None)
    assert work.cancelled


def test_a_lost_lease_wins_over_a_halt_at_the_same_moment() -> None:
    """The halt's own write would be refused: the job is dropped, not ended."""
    work = Work()

    async def go() -> None:
        halt, held = Halt(JOB), lease()

        def both() -> None:
            halt.set(HaltReason.CANCELLED)
            held.mark_lost()

        asyncio.get_running_loop().call_later(0.05, both)
        await _race(work(), lease=held, halt=halt)

    with pytest.raises(LeaseLost):
        asyncio.run(go())
