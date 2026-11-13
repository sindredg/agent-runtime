"""A worker's own view of its lease, and the heartbeat that renews it.

The database decides who holds a job. This is the worker's estimate of that,
kept on its monotonic clock so that a pause of the worker shows up here
without asking the database. It is checked right before the worker starts
something with an effect outside the database: a tool or a model call.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine

from runtime.store import held
from runtime.store.held import LeaseLost

log = logging.getLogger("runtime.worker")


class Lease:
    """One claim of one job, and until when the worker may rely on it."""

    def __init__(
        self,
        job_id: UUID,
        epoch: int,
        length: timedelta,
        *,
        claimed_at: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """`claimed_at` is the clock's time just before the claim was sent."""
        if length <= timedelta(0):
            raise ValueError("lease length must be positive")
        self.job_id = job_id
        self.epoch = epoch
        self.length = length
        self.clock = clock
        # Counted from when the claim was sent, not from when its answer
        # arrived: the database started the lease somewhere in between, so
        # this deadline is never later than the real one.
        self._deadline = claimed_at + length.total_seconds()
        self._lost = False

    @property
    def lost(self) -> bool:
        """True once the database has refused a write under this lease."""
        return self._lost

    def mark_lost(self) -> None:
        self._lost = True

    def renewed(self, sent_at: float) -> None:
        """A renewal that was sent at `sent_at` succeeded."""
        self._deadline = max(self._deadline, sent_at + self.length.total_seconds())

    def remaining(self) -> float:
        """Seconds until the deadline, 0 when it has passed or the lease is lost."""
        if self._lost:
            return 0.0
        return max(0.0, self._deadline - self.clock())

    def check(self) -> None:
        """Raise LeaseLost unless the worker can still rely on the lease."""
        if self.remaining() <= 0:
            raise LeaseLost(self.job_id, self.epoch)


async def keep_renewing(engine: AsyncEngine, lease: Lease, interval: float) -> None:
    """The heartbeat: renew the lease every `interval` seconds.

    It runs beside the job until it is cancelled, or until the database
    says the lease is gone. A renewal that cannot be sent is logged and tried
    again at the next beat. The lease's own deadline covers that case.
    """
    while True:
        await asyncio.sleep(interval)
        sent_at = lease.clock()
        try:
            # A renewal that hangs must not hold up the next one.
            async with asyncio.timeout(interval):
                await held.renew_lease(
                    engine, job_id=lease.job_id, lease_epoch=lease.epoch, lease=lease.length
                )
        except LeaseLost:
            lease.mark_lost()
            log.warning("lease lost", extra={"job_id": lease.job_id, "lease_epoch": lease.epoch})
            return
        except Exception:
            log.exception("lease renewal failed", extra={"job_id": lease.job_id})
            continue
        lease.renewed(sent_at)
