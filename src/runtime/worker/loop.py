"""The worker's outer loop: claim a job, run it, repeat."""

import asyncio
import logging
from datetime import timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine

from runtime.provider.base import Provider
from runtime.store.jobs import claim_job
from runtime.tools.base import ToolRegistry
from runtime.worker.agent import AgentSettings, run_job

log = logging.getLogger("runtime.worker")


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    """Sleep, but wake at once when `stop` is set."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        return


async def keep_alive(alive_path: Path, stop: asyncio.Event, interval: float) -> None:
    """Touch `alive_path` every `interval` seconds until `stop` is set.

    The container health check reads the file's age. It runs beside the work
    loop, so a long job does not make the worker look dead.
    """
    while not stop.is_set():
        # Blocking, but it is one metadata write to a tmpfs file every few seconds.
        alive_path.touch()  # noqa: ASYNC240
        await _wait(stop, interval)


async def work(
    engine: AsyncEngine,
    provider: Provider,
    tools: ToolRegistry,
    settings: AgentSettings,
    *,
    worker_id: str,
    poll_interval: float,
    lease: timedelta,
    stop: asyncio.Event,
) -> None:
    """Claim and run jobs one at a time until `stop` is set.

    `stop` is looked at between jobs. A job that is running is finished
    first.
    """
    while not stop.is_set():
        try:
            async with engine.begin() as conn:
                job = await claim_job(conn, worker_id=worker_id, lease=lease)
        except Exception:
            # The database may be restarting. Keep the worker up and try again.
            log.exception("claim failed")
            await _wait(stop, poll_interval)
            continue
        if job is None:
            await _wait(stop, poll_interval)
            continue
        log.info("job claimed", extra={"job_id": job.id, "lease_epoch": job.lease_epoch})
        try:
            await run_job(engine, provider, tools, job, settings)
        except Exception:
            # run_job records a job's own failure. This is a failure to
            # record it; the job stays `running`.
            log.exception("job could not be finished", extra={"job_id": job.id})
