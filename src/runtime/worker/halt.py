"""Why a job must stop now: its cancel was requested, or its deadline passed (ADR 0010).

One signal for both. A model call in flight is given up and a running tool
container is killed; the job then ends as `cancelled` or `failed`. A worker
that is told to stop (SIGTERM) or loses its lease has its own paths and is
not a halt of the job.
"""

import asyncio
import logging
from enum import StrEnum
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine

from runtime.store.jobs import cancel_wanted

log = logging.getLogger("runtime.worker")

# How often the worker asks whether a cancel was requested. A cancel takes
# effect within this, plus the time to stop what runs.
CANCEL_POLL = 1.0


class HaltReason(StrEnum):
    CANCELLED = "cancelled"
    DEADLINE = "deadline"


class Halted(Exception):
    """The job was halted. Raised where the worker was when it learned of it."""

    def __init__(self, reason: HaltReason) -> None:
        super().__init__(f"the job was halted: {reason.value}")
        self.reason = reason


class Halt:
    """Set once, with the first reason that came."""

    def __init__(self) -> None:
        self.reason: HaltReason | None = None
        self._event = asyncio.Event()

    def set(self, reason: HaltReason) -> None:
        if self.reason is None:
            self.reason = reason
            self._event.set()
            log.info("job halted", extra={"reason": reason.value})

    async def wait(self) -> None:
        await self._event.wait()

    def check(self) -> None:
        """Raise Halted if the job was halted."""
        if self.reason is not None:
            raise Halted(self.reason)


async def watch_cancel(
    engine: AsyncEngine, job_id: UUID, halt: Halt, interval: float = CANCEL_POLL
) -> None:
    """Set `halt` once a cancel of the job is requested. Runs until it is, or is cancelled.

    A read that fails is logged and tried again: the lease covers a worker
    that has lost the database.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            async with engine.connect() as conn:
                wanted = await cancel_wanted(conn, job_id)
        except Exception:
            log.exception("cancel check failed", extra={"job_id": job_id})
            continue
        if wanted:
            halt.set(HaltReason.CANCELLED)
            return
