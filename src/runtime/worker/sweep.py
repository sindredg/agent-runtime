"""Remove the workspaces that no worker will remove: those of final and unknown jobs.

The worker that makes a job final removes its workspace. Some are left
behind: the write that made the job final reached the database but its
answer did not reach the worker, or the job finished on a worker with
another workspace root. Every worker therefore sweeps its workspace root
now and then. A final job is never claimed again, so its directory is not
in use and cannot come back.
"""

import asyncio
import logging
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from runtime.store.schema import jobs
from runtime.store.status import FINAL_STATUSES
from runtime.worker.workspace import remove_workspace

log = logging.getLogger("runtime.worker")


def _job_id(name: str) -> UUID | None:
    """The job a directory name belongs to: `<id>` or the staging name `.<id>.new`."""
    if name.startswith(".") and name.endswith(".new"):
        name = name[1:-4]
    try:
        job_id = UUID(name)
    except ValueError:
        return None
    # Only the canonical spelling is a job's directory.
    return job_id if str(job_id) == name else None


def workspace_jobs(root: Path) -> set[UUID]:
    """The jobs that have a workspace or a staging directory under `root`."""
    try:
        entries = list(root.iterdir())
    except FileNotFoundError:
        return set()
    found = set()
    for entry in entries:
        job_id = _job_id(entry.name)
        if job_id is not None and entry.is_dir() and not entry.is_symlink():
            found.add(job_id)
    return found


async def open_jobs(engine: AsyncEngine, ids: set[UUID]) -> set[UUID]:
    """Those of `ids` that are jobs and not final."""
    if not ids:
        return set()
    final = [status.value for status in FINAL_STATUSES]
    async with engine.connect() as conn:
        result = await conn.execute(
            select(jobs.c.id).where(jobs.c.id.in_(ids), jobs.c.status.not_in(final))
        )
        return set(result.scalars())


async def sweep_workspaces(engine: AsyncEngine, root: Path) -> int:
    """Remove the workspaces under `root` of jobs that are final or unknown. Returns how many."""
    found = await asyncio.to_thread(workspace_jobs, root)
    keep = await open_jobs(engine, found)
    removed = 0
    for job_id in sorted(found - keep):
        await asyncio.to_thread(remove_workspace, root, job_id)
        removed += 1
    if removed:
        log.info("workspaces swept", extra={"removed": removed})
    return removed


async def keep_sweeping(
    engine: AsyncEngine, root: Path, stop: asyncio.Event, interval: float
) -> None:
    """Sweep every `interval` seconds until `stop` is set. A failed sweep is logged and retried."""
    while not stop.is_set():
        try:
            await sweep_workspaces(engine, root)
        except Exception:
            log.exception("workspace sweep failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            continue
