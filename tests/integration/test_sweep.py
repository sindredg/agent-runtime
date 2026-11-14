"""The workspace sweep against real Postgres: issue 9, case 1, and what it must keep.

Case 1: the write that made a job final reached the database, but its answer
did not reach the worker. The worker dropped the job and removed nothing, and
no worker claims a final job again. Only the sweep removes that workspace.
"""

import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine

from runtime.provider.messages import AnthropicProvider
from runtime.store import held
from runtime.store.jobs import claim_job
from runtime.store.status import JobStatus
from runtime.worker.sweep import sweep_workspaces
from runtime.worker.workspace import prepare_workspace

from .conftest import Stack


def test_the_sweep_removes_what_no_worker_will_and_keeps_the_rest(stack: Stack) -> None:
    root = stack.workspace_root

    async def body(engine: AsyncEngine, _: AnthropicProvider) -> tuple[int, dict[str, Path]]:
        async with engine.begin() as conn:
            lost_answer = await stack.create(conn, {"prompt": "finished, answer lost"})
            running = await stack.create(conn, {"prompt": "still running"})
            claimed = await claim_job(conn, worker_id="w1")
            claimed_too = await claim_job(conn, worker_id="w2")
        assert claimed is not None and claimed_too is not None
        assert {claimed.id, claimed_too.id} == {lost_answer.id, running.id}
        held_lost = claimed if claimed.id == lost_answer.id else claimed_too
        paths = {
            "final": prepare_workspace(root, lost_answer.id, {"a.txt": "x"}),
            "running": prepare_workspace(root, running.id, {"b.txt": "y"}),
            # Half made by a worker that stopped, of a job that is final.
            "final staging": root / f".{lost_answer.id}.new",
            # Of a job the database does not know.
            "unknown": root / str(uuid.uuid4()),
            # Not named after a job at all.
            "other": root / "lost+found",
        }
        for name in ("final staging", "unknown", "other"):
            paths[name].mkdir()
        # The final write went in. Its answer is what got lost: nothing removed.
        await held.leave_running(
            engine,
            job_id=lost_answer.id,
            lease_epoch=held_lost.lease_epoch,
            new_status=JobStatus.SUCCEEDED,
        )
        return await sweep_workspaces(engine, root), paths

    removed, paths = stack.run(body)

    assert removed == 2
    assert not paths["final"].exists()
    assert not paths["final staging"].exists()
    assert not paths["unknown"].exists()
    assert paths["running"].is_dir()
    assert (paths["running"] / "b.txt").read_text() == "y"
    assert paths["other"].is_dir()


def test_a_sweep_of_a_root_that_does_not_exist_does_nothing(stack: Stack) -> None:
    async def body(engine: AsyncEngine, _: AnthropicProvider) -> int:
        return await sweep_workspaces(engine, stack.workspace_root)

    assert stack.run(body) == 0
