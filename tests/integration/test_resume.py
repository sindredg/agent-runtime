"""A second worker takes over a job that the first one abandoned, at different points.

Real Postgres. The provider and the tool are stand-ins that keep their own
records: every request the provider received and every run of the tool. Those
records are what the tests check, not the step log alone.

The first worker is abandoned by cancelling it at a chosen point. From then on
it writes nothing, like a process that was killed.
"""

import asyncio
import copy
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from runtime.provider.base import ModelRequest, ModelResponse
from runtime.provider.messages import AnthropicProvider
from runtime.store import held
from runtime.store.jobs import Job, approve_job, claim_job, get_job
from runtime.store.status import JobStatus
from runtime.store.steps import Step, StepKind, read_steps
from runtime.tools import default_tools
from runtime.tools.base import CheckAnswer, CheckOutcome, Tool, ToolCall, ToolRegistry, ToolResult
from runtime.worker.agent import run_job

from .conftest import Stack, expire_lease

K = StepKind
S = JobStatus

# One response with two tool calls, then the final answer.
TURNS: list[list[dict[str, Any]]] = [
    [
        {"type": "text", "text": "Two things to do."},
        {"type": "tool_use", "id": "toolu_a", "name": "act", "input": {"path": "a"}},
        {"type": "tool_use", "id": "toolu_b", "name": "act", "input": {"path": "b"}},
    ],
    [{"type": "text", "text": "Both done."}],
]
SPEC = {"prompt": "do both", "files": {"seed.txt": "seeded\n"}}


class Script:
    """A provider that answers from TURNS and records every request it received.

    The turn is picked by the number of assistant messages in the request,
    like the fake provider does. `hang_at` names a request, counted from 1,
    that is never answered.
    """

    def __init__(self, hang_at: int | None = None) -> None:
        self.requests: list[list[dict[str, Any]]] = []
        self.hang_at = hang_at
        self.hanging = asyncio.Event()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(copy.deepcopy(request.messages))
        if len(self.requests) == self.hang_at:
            self.hanging.set()
            await asyncio.Event().wait()
        turn = sum(1 for message in request.messages if message["role"] == "assistant")
        content = TURNS[turn]
        calls = any(block["type"] == "tool_use" for block in content)
        return ModelResponse(
            f"msg_{turn}", "m", content, "tool_use" if calls else "end_turn", 10, 5
        )

    async def aclose(self) -> None:
        return None


@dataclass
class Acts:
    """The tool `act`, with its own record of every run and every check.

    `hang_at` names a run, counted from 1, that does not return until
    `release` is set.
    """

    idempotent: bool = False
    check_answer: CheckAnswer | Exception | None = None
    hang_at: int | None = None
    runs: list[tuple[str, str]] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)
    hanging: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)

    def run(self, call: ToolCall) -> ToolResult:
        self.runs.append((call.operation_id, call.input["path"]))
        if len(self.runs) == self.hang_at:
            self.hanging.set()
            self.release.wait(timeout=60)
        return ToolResult(f"did {call.input['path']}")

    def check(self, call: ToolCall) -> CheckAnswer:
        self.checks.append(call.operation_id)
        if isinstance(self.check_answer, Exception):
            raise self.check_answer
        assert self.check_answer is not None
        return self.check_answer

    def registry(self) -> ToolRegistry:
        schema = {"type": "object", "properties": {"path": {"type": "string"}}}
        check = None if self.check_answer is None else self.check
        tool = Tool("act", "Do it.", schema, self.run, idempotent=self.idempotent, check=check)
        return ToolRegistry([tool])


@dataclass
class Resumed:
    job: Job
    steps: list[Step]
    second_status: JobStatus | None
    requests: list[list[dict[str, Any]]]
    workspace: Path

    @property
    def kinds(self) -> list[tuple[StepKind, int]]:
        return [(step.kind, step.lease_epoch) for step in self.steps]


def abandon_then_take_over(
    stack: Stack,
    acts: Acts,
    *,
    provider_hangs_at: int | None = None,
    second_root: Path | None = None,
    spec: dict[str, Any] = SPEC,
) -> Resumed:
    """The first worker runs until the provider or the tool hangs, and is abandoned there.

    The lease is then expired, a second worker claims the job and runs it
    with the same tool and provider, which no longer hang.
    """
    script = Script(hang_at=provider_hangs_at)

    async def body(engine: AsyncEngine, _: AnthropicProvider) -> Resumed:
        async with engine.begin() as conn:
            await stack.create(conn, spec)
            first_claim = await claim_job(conn, worker_id="w1")
        assert first_claim is not None
        first = asyncio.create_task(
            run_job(engine, script, acts.registry(), first_claim, stack.settings())
        )
        async with asyncio.timeout(30):
            if provider_hangs_at is not None:
                await script.hanging.wait()
            else:
                await asyncio.to_thread(acts.hanging.wait, 30)
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        # The abandoned tool run ends at some point. Its result goes nowhere.
        acts.hang_at = None
        acts.release.set()
        script.hang_at = None

        async with engine.begin() as conn:
            await expire_lease(conn, first_claim.id)
            second_claim = await claim_job(conn, worker_id="w2")
        assert second_claim is not None
        assert (second_claim.id, second_claim.lease_epoch, second_claim.takeovers) == (
            first_claim.id,
            2,
            1,
        )
        settings = stack.settings()
        if second_root is not None:
            settings = type(settings)(
                default_model=settings.default_model,
                max_tokens=settings.max_tokens,
                workspace_root=second_root,
            )
        second_status = await run_job(engine, script, acts.registry(), second_claim, settings)
        async with engine.begin() as conn:
            job = await get_job(conn, first_claim.id)
            assert job is not None
            steps = await read_steps(conn, job.id)
        return Resumed(
            job, steps, second_status, script.requests, settings.workspace_root / str(job.id)
        )

    return stack.run(body)


def uninterrupted(stack: Stack) -> list[dict[str, Any]]:
    """The last request of the same job run by one worker with no interruption."""
    script = Script()

    async def body(engine: AsyncEngine, _: AnthropicProvider) -> None:
        async with engine.begin() as conn:
            await stack.create(conn, SPEC)
            job = await claim_job(conn, worker_id="w0")
        assert job is not None
        status = await run_job(
            engine, script, Acts(idempotent=True).registry(), job, stack.settings()
        )
        assert status is S.SUCCEEDED

    stack.run(body)
    return script.requests[-1]


def op(job: Job, tool_use_id: str) -> str:
    return f"{job.id}:{tool_use_id}"


# Abandoned at three points, with a tool that may simply run again


def test_a_job_abandoned_during_a_model_call_is_finished_by_a_second_worker(stack: Stack) -> None:
    acts = Acts(idempotent=True)
    resumed = abandon_then_take_over(stack, acts, provider_hangs_at=1)

    assert resumed.second_status is S.SUCCEEDED
    assert resumed.job.status is S.SUCCEEDED
    # The request without a response is sent again, by the second worker.
    assert resumed.kinds == [
        (K.MODEL_REQUEST, 1),
        (K.MODEL_REQUEST, 2),
        (K.MODEL_RESPONSE, 2),
        (K.TOOL_INTENT, 2),
        (K.TOOL_RESULT, 2),
        (K.TOOL_INTENT, 2),
        (K.TOOL_RESULT, 2),
        (K.MODEL_REQUEST, 2),
        (K.MODEL_RESPONSE, 2),
    ]
    assert [step.seq for step in resumed.steps] == list(range(1, 10))
    assert acts.runs == [(op(resumed.job, "toolu_a"), "a"), (op(resumed.job, "toolu_b"), "b")]
    # The provider got the first request twice, and the same conversation
    # in the end as from a job nobody interrupted.
    assert resumed.requests[0] == resumed.requests[1]
    assert resumed.requests[-1] == uninterrupted(stack)


def test_a_job_abandoned_during_a_tool_run_is_finished_by_a_second_worker(stack: Stack) -> None:
    acts = Acts(idempotent=True, hang_at=1)
    resumed = abandon_then_take_over(stack, acts)

    assert resumed.job.status is S.SUCCEEDED
    # The interrupted call gets a new intent of its own. No check was needed.
    assert resumed.kinds == [
        (K.MODEL_REQUEST, 1),
        (K.MODEL_RESPONSE, 1),
        (K.TOOL_INTENT, 1),
        (K.TOOL_INTENT, 2),
        (K.TOOL_RESULT, 2),
        (K.TOOL_INTENT, 2),
        (K.TOOL_RESULT, 2),
        (K.MODEL_REQUEST, 2),
        (K.MODEL_RESPONSE, 2),
    ]
    assert resumed.steps[2].payload == resumed.steps[3].payload
    a, b = op(resumed.job, "toolu_a"), op(resumed.job, "toolu_b")
    # Both runs of the first call carried the same operation id.
    assert acts.runs == [(a, "a"), (a, "a"), (b, "b")]
    assert acts.checks == []
    # The model was asked once before and once after: the response was not asked for again.
    assert len(resumed.requests) == 2
    assert resumed.requests[-1] == uninterrupted(stack)


def test_only_the_second_of_two_calls_is_rerun_when_it_was_the_one_interrupted(
    stack: Stack,
) -> None:
    acts = Acts(idempotent=True, hang_at=2)
    resumed = abandon_then_take_over(stack, acts)

    assert resumed.job.status is S.SUCCEEDED
    assert resumed.kinds == [
        (K.MODEL_REQUEST, 1),
        (K.MODEL_RESPONSE, 1),
        (K.TOOL_INTENT, 1),
        (K.TOOL_RESULT, 1),
        (K.TOOL_INTENT, 1),
        (K.TOOL_INTENT, 2),
        (K.TOOL_RESULT, 2),
        (K.MODEL_REQUEST, 2),
        (K.MODEL_RESPONSE, 2),
    ]
    a, b = op(resumed.job, "toolu_a"), op(resumed.job, "toolu_b")
    # The first call ran once: its stored result was kept.
    assert acts.runs == [(a, "a"), (b, "b"), (b, "b")]
    assert resumed.requests[-1] == uninterrupted(stack)


# The other rows of the decision table: a tool that must not simply run again


def test_a_call_the_check_finds_is_not_run_again(stack: Stack) -> None:
    acts = Acts(check_answer=ToolResult("found: a was done"), hang_at=1)
    resumed = abandon_then_take_over(stack, acts)

    assert resumed.job.status is S.SUCCEEDED
    assert resumed.kinds[:4] == [
        (K.MODEL_REQUEST, 1),
        (K.MODEL_RESPONSE, 1),
        (K.TOOL_INTENT, 1),
        (K.TOOL_RESULT, 2),
    ]
    assert resumed.steps[3].payload == {
        "tool_use_id": "toolu_a",
        "content": "found: a was done",
        "is_error": False,
        "recovered": True,
    }
    a, b = op(resumed.job, "toolu_a"), op(resumed.job, "toolu_b")
    # One run of the first call, the abandoned one. The check was asked once, about it.
    assert acts.runs == [(a, "a"), (b, "b")]
    assert acts.checks == [a]
    # The model was given the check's result.
    sent = resumed.requests[-1][2]["content"]
    assert [block["content"] for block in sent] == ["found: a was done", "did b"]
    # An ordinary result carries no mark.
    assert "recovered" not in resumed.steps[5].payload


def test_a_call_the_check_says_did_not_happen_is_run_again(stack: Stack) -> None:
    acts = Acts(check_answer=CheckOutcome.NOT_DONE, hang_at=1)
    resumed = abandon_then_take_over(stack, acts)

    assert resumed.job.status is S.SUCCEEDED
    assert resumed.kinds[2:5] == [(K.TOOL_INTENT, 1), (K.TOOL_INTENT, 2), (K.TOOL_RESULT, 2)]
    a, b = op(resumed.job, "toolu_a"), op(resumed.job, "toolu_b")
    assert acts.runs == [(a, "a"), (a, "a"), (b, "b")]
    assert acts.checks == [a]
    assert "recovered" not in resumed.steps[4].payload


@pytest.mark.parametrize(
    ("check_answer", "reason"),
    [
        (CheckOutcome.UNKNOWN, "could not tell"),
        (ConnectionError("tracker unreachable"), "check failed: ConnectionError"),
        (None, "not idempotent and has no check"),
    ],
    ids=["check-unknown", "check-raises", "no-check"],
)
def test_a_call_nobody_can_vouch_for_stops_the_job_for_a_person(
    stack: Stack, check_answer: CheckAnswer | Exception | None, reason: str
) -> None:
    acts = Acts(check_answer=check_answer, hang_at=1)
    resumed = abandon_then_take_over(stack, acts)

    assert resumed.second_status is S.NEEDS_APPROVAL
    job = resumed.job
    assert (job.status, job.lease_owner, job.lease_expires_at, job.error) == (
        S.NEEDS_APPROVAL,
        None,
        None,
        None,
    )
    assert resumed.kinds == [
        (K.MODEL_REQUEST, 1),
        (K.MODEL_RESPONSE, 1),
        (K.TOOL_INTENT, 1),
        (K.APPROVAL_REQUESTED, 2),
    ]
    asked = resumed.steps[3].payload
    assert reason in asked.pop("reason")
    assert asked == {
        "tool_use_id": "toolu_a",
        "name": "act",
        "input": {"path": "a"},
        "operation_id": op(job, "toolu_a"),
    }
    # Nothing ran after the takeover: not the interrupted call, not the one after it.
    assert acts.runs == [(op(job, "toolu_a"), "a")]
    assert len(resumed.requests) == 1
    # The job is not over, so its workspace stays.
    assert (resumed.workspace / "seed.txt").read_text() == "seeded\n"


def test_a_question_that_is_already_in_the_log_is_not_asked_twice(stack: Stack) -> None:
    """A worker stopped after asking and before parking the job. The next one only parks it."""
    acts = Acts(hang_at=1)

    async def body(
        engine: AsyncEngine, _: AnthropicProvider
    ) -> tuple[JobStatus | None, list[Step]]:
        script = Script()
        async with engine.begin() as conn:
            await stack.create(conn, SPEC)
            first_claim = await claim_job(conn, worker_id="w1")
        assert first_claim is not None
        first = asyncio.create_task(
            run_job(engine, script, acts.registry(), first_claim, stack.settings())
        )
        await asyncio.to_thread(acts.hanging.wait, 30)
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        acts.hang_at = None
        acts.release.set()

        statuses = []
        for worker in ("w2", "w3"):
            async with engine.begin() as conn:
                await expire_lease(conn, first_claim.id)
                claim = await claim_job(conn, worker_id=worker)
            assert claim is not None
            statuses.append(await run_job(engine, script, acts.registry(), claim, stack.settings()))
            # Put the job back as if this worker had stopped before parking it.
            async with engine.begin() as conn:
                await conn.exec_driver_sql(
                    "UPDATE jobs SET status = 'running', lease_owner = 'gone',"
                    " lease_expires_at = now() WHERE id = $1",
                    (first_claim.id,),
                )
        async with engine.begin() as conn:
            return statuses[-1], await read_steps(conn, first_claim.id)

    status, steps = stack.run(body)
    assert status is S.NEEDS_APPROVAL
    assert [step.kind for step in steps].count(K.APPROVAL_REQUESTED) == 1
    assert len(acts.runs) == 1


# The workspace of a job that changes hands


def test_a_file_a_tool_wrote_before_the_takeover_is_still_there_after_it(stack: Stack) -> None:
    seen: list[str] = []
    hanging, release = threading.Event(), threading.Event()

    def note(call: ToolCall) -> ToolResult:
        notes = call.workspace / "notes.txt"
        if notes.exists():
            seen.append(notes.read_text())
        else:
            notes.write_text("written by the first run")
            hanging.set()
            release.wait(timeout=60)
        return ToolResult("ok")

    schema = {"type": "object"}
    tools = ToolRegistry([Tool("act", "Do it.", schema, note, idempotent=True)])
    script = Script()

    async def body(engine: AsyncEngine, _: AnthropicProvider) -> JobStatus | None:
        async with engine.begin() as conn:
            await stack.create(conn, SPEC)
            first_claim = await claim_job(conn, worker_id="w1")
        assert first_claim is not None
        first = asyncio.create_task(run_job(engine, script, tools, first_claim, stack.settings()))
        await asyncio.to_thread(hanging.wait, 30)
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        release.set()
        async with engine.begin() as conn:
            await expire_lease(conn, first_claim.id)
            second_claim = await claim_job(conn, worker_id="w2")
        assert second_claim is not None
        return await run_job(engine, script, tools, second_claim, stack.settings())

    assert stack.run(body) is S.SUCCEEDED
    assert seen == ["written by the first run", "written by the first run"]
    # The worker that made the job final removed the workspace.
    assert list(stack.workspace_root.iterdir()) == []


def test_a_worker_with_an_empty_workspace_gets_the_spec_files_again(
    stack: Stack, tmp_path: Path
) -> None:
    """As on another host: nothing of the first worker's directory is there."""
    acts = Acts(idempotent=True, hang_at=1)
    other_root = tmp_path / "another-host"
    seen: list[str] = []
    original_run = acts.run

    def run_and_look(call: ToolCall) -> ToolResult:
        seen.append((call.workspace / "seed.txt").read_text())
        return original_run(call)

    acts.run = run_and_look  # type: ignore[method-assign]
    resumed = abandon_then_take_over(stack, acts, second_root=other_root)
    assert resumed.job.status is S.SUCCEEDED
    assert seen == ["seeded\n"] * 3
    # The second worker removed its own directory. The first worker's is
    # left behind: nothing on that side knows the job ended.
    assert not resumed.workspace.exists()
    assert (stack.workspace_root / str(resumed.job.id) / "seed.txt").is_file()


def test_the_real_read_file_is_rerun_without_a_question(stack: Stack) -> None:
    """`read_file` declares itself idempotent. An interrupted read needs no person."""
    assert default_tools().get("read_file") is not None
    turns: list[list[dict[str, Any]]] = [
        [{"type": "tool_use", "id": "toolu_r", "name": "read_file", "input": {"path": "seed.txt"}}],
        [{"type": "text", "text": "Read."}],
    ]

    class ReadScript(Script):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            self.requests.append(copy.deepcopy(request.messages))
            turn = sum(1 for message in request.messages if message["role"] == "assistant")
            stop = "tool_use" if turn == 0 else "end_turn"
            return ModelResponse(f"msg_{turn}", "m", turns[turn], stop, 1, 1)

    script = ReadScript()

    async def body(
        engine: AsyncEngine, _: AnthropicProvider
    ) -> tuple[JobStatus | None, list[Step]]:
        async with engine.begin() as conn:
            job = await stack.create(conn, SPEC)
            first_claim = await claim_job(conn, worker_id="w1")
        assert first_claim is not None
        # The log a worker leaves when it stops inside the read.
        first = stack.settings()
        await held.append_step(
            engine,
            job_id=job.id,
            lease_epoch=1,
            seq=1,
            kind=K.MODEL_REQUEST,
            payload={"model": "m", "max_tokens": 1, "messages": 1},
        )
        await held.append_step(
            engine,
            job_id=job.id,
            lease_epoch=1,
            seq=2,
            kind=K.MODEL_RESPONSE,
            payload={"id": "msg_0", "content": turns[0], "stop_reason": "tool_use", "usage": {}},
        )
        await held.append_step(
            engine,
            job_id=job.id,
            lease_epoch=1,
            seq=3,
            kind=K.TOOL_INTENT,
            payload={"tool_use_id": "toolu_r", "name": "read_file", "input": {"path": "seed.txt"}},
        )
        async with engine.begin() as conn:
            await expire_lease(conn, job.id)
            second_claim = await claim_job(conn, worker_id="w2")
        assert second_claim is not None
        status = await run_job(engine, script, default_tools(), second_claim, first)
        async with engine.begin() as conn:
            return status, await read_steps(conn, job.id)

    status, steps = stack.run(body)
    assert status is S.SUCCEEDED
    assert [step.kind for step in steps[2:5]] == [K.TOOL_INTENT, K.TOOL_INTENT, K.TOOL_RESULT]
    assert steps[4].payload["content"] == "seeded\n"
    assert script.requests[-1][2]["content"][0]["content"] == "seeded\n"


# Approval: a person says the call may run again


def test_an_approved_call_is_run_again_under_its_own_intent(stack: Stack) -> None:
    acts = Acts(hang_at=1)
    script = Script()

    async def body(engine: AsyncEngine, _: AnthropicProvider) -> tuple[Job, Job, list[Step]]:
        async with engine.begin() as conn:
            await stack.create(conn, SPEC)
            first_claim = await claim_job(conn, worker_id="w1")
        assert first_claim is not None
        first = asyncio.create_task(
            run_job(engine, script, acts.registry(), first_claim, stack.settings())
        )
        await asyncio.to_thread(acts.hanging.wait, 30)
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        acts.hang_at = None
        acts.release.set()

        async with engine.begin() as conn:
            await expire_lease(conn, first_claim.id)
            second_claim = await claim_job(conn, worker_id="w2")
        assert second_claim is not None
        parked = await run_job(engine, script, acts.registry(), second_claim, stack.settings())
        assert parked is S.NEEDS_APPROVAL
        # Nobody may claim a job that waits for a person.
        async with engine.begin() as conn:
            assert await claim_job(conn, worker_id="w3") is None
            await approve_job(conn, first_claim.id)
        async with engine.begin() as conn:
            third_claim = await claim_job(conn, worker_id="w3")
        assert third_claim is not None
        status = await run_job(engine, script, acts.registry(), third_claim, stack.settings())
        assert status is S.SUCCEEDED
        async with engine.begin() as conn:
            job = await get_job(conn, first_claim.id)
            assert job is not None
            return third_claim, job, await read_steps(conn, job.id)

    third_claim, job, steps = stack.run(body)
    # The claim after the approval is an ordinary claim, not a takeover.
    assert (third_claim.lease_epoch, third_claim.takeovers) == (3, 1)
    assert job.status is S.SUCCEEDED
    assert [(step.kind, step.lease_epoch) for step in steps] == [
        (K.MODEL_REQUEST, 1),
        (K.MODEL_RESPONSE, 1),
        (K.TOOL_INTENT, 1),
        (K.APPROVAL_REQUESTED, 2),
        (K.APPROVAL_GIVEN, 2),
        (K.TOOL_INTENT, 3),
        (K.TOOL_RESULT, 3),
        (K.TOOL_INTENT, 3),
        (K.TOOL_RESULT, 3),
        (K.MODEL_REQUEST, 3),
        (K.MODEL_RESPONSE, 3),
    ]
    a, b = op(job, "toolu_a"), op(job, "toolu_b")
    # Two runs of the first call, for two intents: the abandoned one and the approved one.
    assert acts.runs == [(a, "a"), (a, "a"), (b, "b")]
    assert steps[4].payload == {"tool_use_id": "toolu_a", "operation_id": a}


def test_an_approved_rerun_that_is_interrupted_too_needs_a_new_approval(stack: Stack) -> None:
    """Approval covers one run. The intent of the rerun is a new open question."""
    acts = Acts(hang_at=1)
    script = Script()

    async def body(
        engine: AsyncEngine, _: AnthropicProvider
    ) -> tuple[JobStatus | None, list[Step]]:
        async with engine.begin() as conn:
            job = await stack.create(conn, SPEC)
        claims = 0

        async def claim() -> Job:
            nonlocal claims
            claims += 1
            async with engine.begin() as conn:
                await expire_lease(conn, job.id)
                claimed = await claim_job(conn, worker_id=f"w{claims}")
            assert claimed is not None
            return claimed

        async def abandon_in_the_tool() -> None:
            acts.hang_at = len(acts.runs) + 1
            acts.hanging.clear()
            acts.release.clear()
            worker = asyncio.create_task(
                run_job(engine, script, acts.registry(), await claim(), stack.settings())
            )
            await asyncio.to_thread(acts.hanging.wait, 30)
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            acts.hang_at = None
            acts.release.set()

        await abandon_in_the_tool()
        assert (
            await run_job(engine, script, acts.registry(), await claim(), stack.settings())
            is S.NEEDS_APPROVAL
        )
        async with engine.begin() as conn:
            await approve_job(conn, job.id)
        # The approved rerun starts, and its worker stops inside the tool again.
        await abandon_in_the_tool()
        status = await run_job(engine, script, acts.registry(), await claim(), stack.settings())
        async with engine.begin() as conn:
            return status, await read_steps(conn, job.id)

    status, steps = stack.run(body)
    assert status is S.NEEDS_APPROVAL
    assert [step.kind for step in steps][2:] == [
        K.TOOL_INTENT,
        K.APPROVAL_REQUESTED,
        K.APPROVAL_GIVEN,
        K.TOOL_INTENT,
        K.APPROVAL_REQUESTED,
    ]
    assert len(acts.runs) == 2
