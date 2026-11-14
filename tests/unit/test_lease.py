"""The worker's own view of its lease: a deadline on its monotonic clock."""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest

from runtime.store.held import LeaseLost
from runtime.worker.lease import Lease


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def lease_of(seconds: float, clock: FakeClock, claimed_at: float | None = None) -> Lease:
    return Lease(
        uuid4(),
        3,
        timedelta(seconds=seconds),
        claimed_at=clock.now if claimed_at is None else claimed_at,
        clock=clock,
    )


def test_a_lease_is_live_until_its_length_after_the_claim_was_sent() -> None:
    clock = FakeClock()
    lease = lease_of(30, clock)
    lease.check()
    clock.now += 29.9
    lease.check()
    assert lease.remaining() == pytest.approx(0.1)


@pytest.mark.parametrize("late_by", [0.0, 0.1, 3600.0])
def test_a_lease_is_over_at_its_deadline_and_after(late_by: float) -> None:
    clock = FakeClock()
    lease = lease_of(30, clock)
    clock.now += 30 + late_by
    with pytest.raises(LeaseLost) as lost:
        lease.check()
    assert (lost.value.job_id, lost.value.lease_epoch) == (lease.job_id, 3)
    assert lease.remaining() == 0.0


def test_the_deadline_counts_from_when_the_claim_was_sent_not_from_its_answer() -> None:
    """The claim took 5 seconds to come back. Those seconds are gone from the lease."""
    clock = FakeClock()
    lease = lease_of(30, clock, claimed_at=clock.now - 5)
    assert lease.remaining() == pytest.approx(25)


def test_a_renewal_runs_the_lease_from_when_the_renewal_was_sent() -> None:
    clock = FakeClock()
    lease = lease_of(30, clock)
    clock.now += 10
    sent_at = clock()
    # The answer arrives 4 seconds later. The database counted from before that.
    clock.now += 4
    lease.renewed(sent_at)
    assert lease.remaining() == pytest.approx(26)
    clock.now += 25.9
    lease.check()
    clock.now += 0.2
    with pytest.raises(LeaseLost):
        lease.check()


def test_an_answer_that_arrives_out_of_order_does_not_shorten_the_lease() -> None:
    clock = FakeClock()
    lease = lease_of(30, clock)
    clock.now += 20
    lease.renewed(clock())
    lease.renewed(clock() - 15)
    assert lease.remaining() == pytest.approx(30)


def test_a_lease_marked_lost_is_over_whatever_the_clock_says() -> None:
    clock = FakeClock()
    lease = lease_of(30, clock)
    assert not lease.lost
    lease.mark_lost()
    assert lease.lost
    with pytest.raises(LeaseLost):
        lease.check()
    # A renewal that was already on its way does not bring it back.
    lease.renewed(clock())
    with pytest.raises(LeaseLost):
        lease.check()


def test_a_lease_must_have_a_positive_length() -> None:
    with pytest.raises(ValueError, match="positive"):
        lease_of(0, FakeClock())


def test_waiting_for_the_loss_ends_when_the_database_refused_the_lease() -> None:
    async def body() -> bool:
        lease = lease_of(30, FakeClock())
        waiting = asyncio.ensure_future(lease.wait_lost())
        await asyncio.sleep(0.01)
        before = waiting.done()
        lease.mark_lost()
        async with asyncio.timeout(1):
            await waiting
        return before

    assert asyncio.run(body()) is False


def test_waiting_for_the_loss_does_not_end_when_only_the_deadline_passed() -> None:
    """Past the deadline the worker starts nothing new, but the job may still be its own.

    A model answer that arrives then is still stored if nobody took the job,
    so a call in flight is not given up for the deadline alone.
    """

    async def body() -> bool:
        clock = FakeClock()
        lease = lease_of(30, clock)
        waiting = asyncio.ensure_future(lease.wait_lost())
        clock.now += 3600
        await asyncio.sleep(0.01)
        done = waiting.done()
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        return done

    assert asyncio.run(body()) is False
