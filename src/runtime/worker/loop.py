"""The worker's outer loop: claim a job, run it, repeat."""

import asyncio
import logging
import time
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine

from runtime.provider.base import Provider
from runtime.store.jobs import Job, claim_job
from runtime.tools.base import ToolRegistry
from runtime.worker.agent import AgentSettings, run_job
from runtime.worker.workspace import remove_workspace

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
    stop: asyncio.Event,
) -> None:
    """Claim and run jobs one at a time until `stop` is set.

    Once `stop` is set, no further job is claimed. The job in hand is
    released at its next step boundary.
    """
    while not stop.is_set():
        # Taken before the claim is sent: the worker's deadline for the
        # lease counts from here, so it is never later than the database's.
        claimed_at = time.monotonic()
        failed: list[Job] = []
        try:
            async with engine.begin() as conn:
                job = await claim_job(
                    conn, worker_id=worker_id, lease=settings.lease, failed=failed
                )
            # Jobs the claim failed at the takeover limit are final, and this
            # worker made them so. Their workspaces go, like any finished job's.
            for ended in failed:
                await asyncio.to_thread(remove_workspace, settings.workspace_root, ended.id)
        except Exception:
            # The database may be restarting. Keep the worker up and try again.
            log.exception("claim failed")
            await _wait(stop, poll_interval)
            continue
        if job is None:
            await _wait(stop, poll_interval)
            continue
        log.info(
            "job claimed",
            extra={"job_id": job.id, "lease_epoch": job.lease_epoch, "takeovers": job.takeovers},
        )
        try:
            await run_job(engine, provider, tools, job, settings, claimed_at=claimed_at, stop=stop)
        except Exception:
            # run_job records a job's own failure and drops a job it can no
            # longer write to. This is anything else; the job stays `running`
            # until its lease runs out.
            log.exception("job could not be finished", extra={"job_id": job.id})
