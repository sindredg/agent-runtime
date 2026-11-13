"""A worker and its lease: the heartbeat, the local deadline, and giving a job up.

Real Postgres and the fake provider. Leases are a second or less here so
that the tests can outlast them.
"""

import asyncio
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import asyncpg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from runtime.provider.base import ModelRequest, ModelResponse
from runtime.provider.messages import AnthropicProvider
from runtime.store.jobs import Job, claim_job, get_job
from runtime.store.status import JobStatus
from runtime.store.steps import Step, StepKind, read_steps
from runtime.tools.base import ToolRegistry, ToolResult
from runtime.worker.agent import run_job

from .conftest import FIXED, HELLO, Stack, stub_tool

K = StepKind
S = JobStatus


async def claim(
    engine: AsyncEngine, stack: Stack, worker_id: str, lease: float
) -> tuple[Job, float]:
    """Create the hello job and claim it. Returns the job and when the claim was sent."""
    async with engine.begin() as conn:
        await stack.create(conn, {**HELLO, "files": {"seed.txt": "x"}})
        sent_at = time.monotonic()
        job = await claim_job(conn, worker_id=worker_id, lease=timedelta(seconds=lease))
    assert job is not None
    return job, sent_at


async def read(engine: AsyncEngine, job: Job) -> tuple[Job, list[Step]]:
    async with engine.begin() as conn:
        found = await get_job(conn, job.id)
        assert found is not None
        return found, await read_steps(conn, job.id)


def test_the_heartbeat_keeps_a_job_that_runs_longer_than_its_lease(stack: Stack) -> None:
    """The tool takes 2.5 s under a 1 s lease. A second worker polls throughout and gets nothing."""

    def slow(workspace: Path, tool_input: dict[str, Any]) -> ToolResult:
        time.sleep(2.5)
        return ToolResult("slow but done")

    async def body(
        engine: AsyncEngine, provider: AnthropicProvider
    ) -> tuple[JobStatus | None, list[Job | None], Job]:
        job, sent_at = await claim(engine, stack, "w1", lease=1.0)
        rival_claims: list[Job | None] = []

        async def rival() -> None:
            while True:
                async with engine.begin() as conn:
                    rival_claims.append(await claim_job(conn, worker_id="w2"))
                await asyncio.sleep(0.1)

        polling = asyncio.create_task(rival())
        try:
            status = await run_job(
                engine,
                provider,
                stub_tool(slow),
                job,
                stack.settings(lease=1.0, heartbeat=0.2),
                claimed_at=sent_at,
            )
        finally:
            polling.cancel()
            await asyncio.gather(polling, return_exceptions=True)
        return status, rival_claims, (await read(engine, job))[0]

    status, rival_claims, job = stack.run(body)
    assert status is S.SUCCEEDED
    assert len(rival_claims) > 10
    assert all(claimed is None for claimed in rival_claims)
    assert (job.status, job.lease_epoch, job.takeovers) == (S.SUCCEEDED, 1, 0)


def test_a_worker_whose_heartbeat_stopped_loses_the_job_and_writes_nothing_more(
    stack: Stack,
) -> None:
    """The first worker sits in a tool past its lease and never renews.

    A second worker's claim wins. When the first worker's tool returns, its
    result is refused, it leaves the status and the workspace alone, and the
    second worker finishes the job.
    """
    started, release = threading.Event(), threading.Event()

    def blocks(workspace: Path, tool_input: dict[str, Any]) -> ToolResult:
        started.set()
        release.wait(timeout=30)
        return ToolResult("too late")

    async def body(engine: AsyncEngine, provider: AnthropicProvider) -> dict[str, Any]:
        job, sent_at = await claim(engine, stack, "w1", lease=1.0)
        first = asyncio.create_task(
            run_job(
                engine,
                provider,
                stub_tool(blocks),
                job,
                # An hour between renewals: the heartbeat never comes.
                stack.settings(lease=1.0, heartbeat=3600),
                claimed_at=sent_at,
            )
        )
        await asyncio.to_thread(started.wait, 20)
        async with asyncio.timeout(20):
            taken = None
            while taken is None:
                await asyncio.sleep(0.1)
                async with engine.begin() as conn:
                    taken = await claim_job(conn, worker_id="w2")
        _, before = await read(engine, job)
        release.set()
        first_status = await first
        held_by_second, after = await read(engine, job)
        workspace = stack.workspace_root / str(job.id)
        workspace_kept = (workspace / "seed.txt").is_file()

        second_status = await run_job(engine, provider, FIXED, taken, stack.settings())
        final, steps = await read(engine, job)
        return {
            "taken": taken,
            "first_status": first_status,
            "before": before,
            "after": after,
            "held_by_second": held_by_second,
            "workspace_kept": workspace_kept,
            "second_status": second_status,
            "final": final,
            "steps": steps,
            "workspace_left": workspace.exists(),
        }

    seen = stack.run(body)
    taken: Job = seen["taken"]
    assert (taken.lease_owner, taken.lease_epoch, taken.takeovers) == ("w2", 2, 1)
    # The first worker dropped the job: no status, no step, workspace untouched.
    assert seen["first_status"] is None
    assert seen["after"] == seen["before"]
    assert [step.kind for step in seen["before"]] == [
        K.MODEL_REQUEST,
        K.MODEL_RESPONSE,
        K.TOOL_INTENT,
    ]
    assert seen["held_by_second"] == taken
    assert seen["workspace_kept"]
    # The second worker finished it, and only then was the workspace removed.
    assert seen["second_status"] is S.SUCCEEDED
    assert seen["final"].status is S.SUCCEEDED
    assert {step.lease_epoch for step in seen["steps"][3:]} == {2}
    assert "too late" not in str([step.payload for step in seen["steps"]])
    assert not seen["workspace_left"]


class SlowToolUse:
    """A provider whose answer, a tool call, takes longer than the worker's lease."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        # A worker that goes on regardless would call again and again. Cut
        # that short, so the test fails instead of hanging.
        if self.calls > 2:
            raise RuntimeError("the model was called again and again")
        await asyncio.sleep(self.seconds)
        call = {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "a"}}
        return ModelResponse("msg_1", "m", [call], "tool_use", 1, 1)

    async def aclose(self) -> None:
        return None


def test_a_worker_past_its_own_deadline_starts_no_tool(stack: Stack) -> None:
    """Nobody took the job over, so the database would still accept the worker's writes.

    The worker's own clock says the lease is over. It must not start the tool.
    """
    ran: list[str] = []

    def record(workspace: Path, tool_input: dict[str, Any]) -> ToolResult:
        ran.append("ran")
        return ToolResult("ok")

    async def body(
        engine: AsyncEngine, _: AnthropicProvider
    ) -> tuple[JobStatus | None, Job, list[Step]]:
        job, sent_at = await claim(engine, stack, "w1", lease=0.5)
        status = await run_job(
            engine,
            SlowToolUse(1.0),
            stub_tool(record),
            job,
            stack.settings(lease=0.5, heartbeat=3600),
            claimed_at=sent_at,
        )
        return status, *await read(engine, job)

    status, job, steps = stack.run(body)
    assert status is None
    assert ran == []
    # No intent either: a call that never started must not look interrupted.
    assert [step.kind for step in steps] == [K.MODEL_REQUEST, K.MODEL_RESPONSE]
    assert (job.status, job.lease_owner, job.error) == (S.RUNNING, "w1", None)


def test_a_worker_past_its_own_deadline_starts_no_model_call(stack: Stack) -> None:
    def slow(workspace: Path, tool_input: dict[str, Any]) -> ToolResult:
        time.sleep(1.0)
        return ToolResult("ok")

    async def body(
        engine: AsyncEngine, provider: AnthropicProvider
    ) -> tuple[JobStatus | None, Job, list[Step]]:
        job, sent_at = await claim(engine, stack, "w1", lease=0.5)
        status = await run_job(
            engine,
            provider,
            stub_tool(slow),
            job,
            stack.settings(lease=0.5, heartbeat=3600),
            claimed_at=sent_at,
        )
        return status, *await read(engine, job)

    status, job, steps = stack.run(body)
    assert status is None
    assert [step.kind for step in steps] == [
        K.MODEL_REQUEST,
        K.MODEL_RESPONSE,
        K.TOOL_INTENT,
        K.TOOL_RESULT,
    ]
    assert job.status is S.RUNNING
    # The provider's own log: the second call was never made.
    assert [request["turn"] for request in stack.requests_for(job)] == [0]


def test_a_database_error_in_the_middle_of_a_job_drops_it_instead_of_failing_it(
    stack: Stack,
) -> None:
    """The tool cuts every session to the database. The worker's next write cannot be sent.

    A job the worker can no longer write to is not the job's failure. It is
    left `running` for another worker to take over when the lease runs out.
    """

    def cut_sessions(workspace: Path, tool_input: dict[str, Any]) -> ToolResult:
        async def cut() -> None:
            conn = await asyncpg.connect(stack.database_url, timeout=5)
            try:
                await conn.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                    " WHERE datname = current_database() AND pid <> pg_backend_pid()"
                )
            finally:
                await conn.close()

        asyncio.run(cut())
        return ToolResult("the database sessions are gone")

    async def body(
        engine: AsyncEngine, provider: AnthropicProvider
    ) -> tuple[JobStatus | None, Job, list[Step], bool]:
        job, sent_at = await claim(engine, stack, "w1", lease=30)
        status = await run_job(
            engine, provider, stub_tool(cut_sessions), job, stack.settings(), claimed_at=sent_at
        )
        await engine.dispose()
        workspace_kept = (stack.workspace_root / str(job.id) / "seed.txt").is_file()
        return status, *await read(engine, job), workspace_kept

    status, job, steps, workspace_kept = stack.run(body)
    assert status is None
    assert (job.status, job.lease_owner, job.error) == (S.RUNNING, "w1", None)
    assert [step.kind for step in steps] == [K.MODEL_REQUEST, K.MODEL_RESPONSE, K.TOOL_INTENT]
    assert workspace_kept


class EndsTheJobItself:
    """A provider that, during the call, moves the job to a final status behind the worker."""

    def attach(self, engine: AsyncEngine, job: Job) -> None:
        self.engine, self.job = engine, job

    async def complete(self, request: ModelRequest) -> ModelResponse:
        async with self.engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE jobs SET status = 'cancelled', lease_owner = NULL,"
                    " lease_expires_at = NULL WHERE id = :id"
                ),
                {"id": self.job.id},
            )
        return ModelResponse("msg_1", "m", [{"type": "text", "text": "hi"}], "end_turn", 1, 1)

    async def aclose(self) -> None:
        return None


def test_a_job_ended_behind_the_worker_is_dropped_and_its_workspace_left_alone(
    stack: Stack,
) -> None:
    """The job is already final when the worker comes back with the model's answer."""

    async def body(
        engine: AsyncEngine, _: AnthropicProvider
    ) -> tuple[JobStatus | None, Job, list[Step]]:
        job, sent_at = await claim(engine, stack, "w1", lease=30)
        provider = EndsTheJobItself()
        provider.attach(engine, job)
        status = await run_job(
            engine, provider, ToolRegistry(), job, stack.settings(), claimed_at=sent_at
        )
        return status, *await read(engine, job)

    status, job, steps = stack.run(body)
    assert status is None
    assert job.status is S.CANCELLED
    assert [step.kind for step in steps] == [K.MODEL_REQUEST]
    assert (stack.workspace_root / str(job.id) / "seed.txt").is_file()
