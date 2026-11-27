"""A job goes on past its context window through summary steps (ADR 0033).

Real Postgres and the stack's fake provider. The `summary` scenario reads
eight notes files, one per turn. With the trigger below it summarises after
the third and the sixth read. The fake's request log is the record of what
the worker really sent.
"""

import asyncio
import re
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from runtime.provider.base import ModelRequest, ModelResponse
from runtime.provider.messages import AnthropicProvider
from runtime.store import held
from runtime.store.jobs import Job, cancel_job, claim_job, get_job
from runtime.store.status import JobStatus
from runtime.store.steps import Step, StepKind, read_steps
from runtime.tools import default_tools
from runtime.tools.base import ToolCall, ToolRegistry, ToolResult
from runtime.tools.trackers import FakeTracker
from runtime.worker.agent import run_job
from runtime.worker.summary import CONTINUATION, SUMMARY_INSTRUCTION

from .conftest import HELLO, Stack, expire_lease, stub_tool

SPEC: dict[str, Any] = {
    "prompt": "Read the notes files a.txt to h.txt in order, then say you are done.",
    "model": "summary",
}
# Held by the test, not read from the fake: the scenario's summary text, and
# the turn each summary is written at.
SUMMARY_TEXT = (
    "The task: read the notes files a.txt to h.txt in order, then say done."
    " Read so far: every file before the turn named below."
)
TRIGGER = 1000
WINDOW_TURNS = [3, 6]
SUMMARIES = len(WINDOW_TURNS)
# The pass line of G7: a halted job stops within 5 s.
WITHIN = 5.0
NONE = {"type": "none"}
# Every other request of a job with a trigger: one tool call at most.
ONE_CALL = {"type": "auto", "disable_parallel_tool_use": True}


def notes(path: str) -> str:
    """The text of one notes file: long enough that three reads reach the trigger."""
    return "".join(f"{path}, line {n}: a note the later steps need.\n" for n in range(40))


TOOLS = stub_tool(lambda call: ToolResult(notes(str(call.input["path"]))))


def fake_summary(number: int) -> str:
    """Summary `number` as the fake writes it."""
    return f"{SUMMARY_TEXT}\n\n[summary {number}] [turn {WINDOW_TURNS[number - 1]}]"


def opening(number: int) -> dict[str, Any]:
    """The message that opens the window after summary `number`."""
    text = f"{fake_summary(number)}\n\n{CONTINUATION}"
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def of_kind(steps: list[Step], kind: StepKind) -> list[Step]:
    return [step for step in steps if step.kind is kind]


def summary_requests(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every request of a job with a trigger that does not ask for one call."""
    return [entry for entry in entries if entry["tool_choice"] != ONE_CALL]


def without_job_id(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The request bodies, without the job id: what two jobs can be compared on."""
    return [
        {key: value for key, value in entry["body"].items() if key != "metadata"}
        for entry in entries
    ]


class Stopping:
    """The real client, which tells the worker to stop at a chosen point.

    After `answers` complete answers, or once a summary request is in the
    fake's log and before its answer, which the fake holds back.
    """

    def __init__(
        self,
        inner: AnthropicProvider,
        stack: Stack,
        *,
        answers: int | None = None,
        in_summary: bool = False,
    ) -> None:
        self.inner, self.stack = inner, stack
        self.answers, self.in_summary = answers, in_summary
        self.stop = asyncio.Event()
        self.seen = 0
        self.job: Job | None = None

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if self.in_summary and request.tool_choice == NONE:
            call = asyncio.ensure_future(self.inner.complete(request))
            try:
                assert self.job is not None
                # The provider's log is read over HTTP: there is no event to wait on.
                while not summary_requests(  # noqa: ASYNC110
                    await asyncio.to_thread(self.stack.requests_for, self.job)
                ):
                    await asyncio.sleep(0.05)
                self.stop.set()
                return await call
            finally:
                call.cancel()
                await asyncio.gather(call, return_exceptions=True)
        answer = await self.inner.complete(request)
        self.seen += 1
        if self.answers is not None and self.seen >= self.answers:
            self.stop.set()
        return answer

    async def aclose(self) -> None:
        return None


async def read(engine: AsyncEngine, job_id: UUID) -> tuple[Job, list[Step]]:
    async with engine.begin() as conn:
        job = await get_job(conn, job_id)
        assert job is not None
        return job, await read_steps(conn, job.id)


async def take_over(engine: AsyncEngine, job_id: UUID) -> Job:
    """A worker claims the job and stops without a word; the next takes it over."""
    async with engine.begin() as conn:
        assert await claim_job(conn, worker_id="stopped-worker") is not None
        await expire_lease(conn, job_id)
        taken = await claim_job(conn, worker_id="next-worker")
    assert taken is not None
    assert taken.id == job_id
    assert taken.takeovers == 1
    return taken


def run_whole(
    stack: Stack, trigger: int | None, spec: dict[str, Any] = SPEC, tools: ToolRegistry = TOOLS
) -> tuple[Job, list[Step]]:
    """One worker runs the job from start to end."""

    async def body(engine: AsyncEngine, provider: AnthropicProvider) -> tuple[Job, list[Step]]:
        async with engine.begin() as conn:
            created = await stack.create(conn, spec)
            claimed = await claim_job(conn, worker_id="only-worker")
        assert claimed is not None
        await run_job(engine, provider, tools, claimed, stack.settings(summary_trigger=trigger))
        return await read(engine, created.id)

    return stack.run(body)


def run_split(
    stack: Stack,
    first_trigger: int | None,
    then_trigger: int | None,
    *,
    answers: int | None = None,
    in_summary: bool = False,
) -> tuple[Job, list[Step], JobStatus | None]:
    """The first worker stops at a chosen point; another, with its own trigger, takes over."""

    async def body(
        engine: AsyncEngine, provider: AnthropicProvider
    ) -> tuple[Job, list[Step], JobStatus | None]:
        stopping = Stopping(provider, stack, answers=answers, in_summary=in_summary)
        async with engine.begin() as conn:
            await stack.create(conn, SPEC)
            first = await claim_job(conn, worker_id="first-worker")
        assert first is not None
        stopping.job = first
        handed_back = await run_job(
            engine,
            stopping,
            TOOLS,
            first,
            stack.settings(summary_trigger=first_trigger),
            stop=stopping.stop,
        )
        taken = await take_over(engine, first.id)
        await run_job(engine, provider, TOOLS, taken, stack.settings(summary_trigger=then_trigger))
        job, steps = await read(engine, first.id)
        return job, steps, handed_back

    return stack.run(body)


# A long job


def test_a_long_job_summarises_twice_and_each_window_opens_from_its_summary(
    stack: Stack,
) -> None:
    job, steps = run_whole(stack, TRIGGER)
    entries = stack.requests_for(job)

    assert job.status == "succeeded", job.error
    assert len(of_kind(steps, StepKind.SUMMARY)) == SUMMARIES
    summaries = summary_requests(entries)
    assert [entry["summary"] for entry in summaries] == [1, 2]
    for entry in summaries:
        index = entries.index(entry)
        before, after = entries[index - 1], entries[index + 1]
        assert entry["tool_choice"] == NONE
        # The tools are kept, and the instruction is the last block.
        assert entry["body"]["tools"] == before["body"]["tools"]
        assert entry["body"]["messages"][-1]["content"][-1] == {
            "type": "text",
            "text": SUMMARY_INSTRUCTION,
        }
        assert entry["body"]["messages"][-1]["content"][-2]["type"] == "tool_result"
        # The first request of the next window is the summary and the continuation line.
        assert after["body"]["messages"] == [opening(entry["summary"])]
    # The script went on after each summary: every turn answered once, in order.
    turns = [entry["turn"] for entry in entries if entry["tool_choice"] == ONE_CALL]
    assert turns == list(range(9))
    # The summary's tokens count like any response's.
    counted = sum(
        step.payload["usage"]["input_tokens"] + step.payload["usage"]["output_tokens"]
        for step in steps
        if step.kind in (StepKind.MODEL_RESPONSE, StepKind.SUMMARY)
    )
    by_fake = sum(sum(entry["usage"].values()) for entry in entries)
    assert job.tokens_used == counted == by_fake


def test_every_request_step_names_the_tools_its_request_offered(stack: Stack) -> None:
    job, steps = run_whole(stack, TRIGGER)
    requests = of_kind(steps, StepKind.MODEL_REQUEST)
    entries = stack.requests_for(job)

    assert len(requests) == len(entries)
    for step, entry in zip(requests, entries, strict=True):
        assert step.payload["tools"] == [tool["name"] for tool in entry["body"]["tools"]]
        assert step.payload["summary_trigger"] == TRIGGER
        assert step.payload.get("summary", False) is (entry["tool_choice"] == NONE)


def test_the_tools_named_are_every_tool_offered_in_order(
    stack: Stack, fake_tracker_url: str
) -> None:
    tools = default_tools(tracker=FakeTracker(fake_tracker_url))

    ran = stack.run_one({**HELLO, "files": {"hello.txt": "hi"}}, tools)

    assert ran.job.status == "succeeded", ran.job.error
    requests = of_kind(ran.steps, StepKind.MODEL_REQUEST)
    assert len(requests) == len(ran.requests)
    for step, entry in zip(requests, ran.requests, strict=True):
        named = [tool["name"] for tool in entry["body"]["tools"]]
        assert len(named) >= 2
        assert step.payload["tools"] == named
        assert step.payload["summary_trigger"] is None


def test_with_no_trigger_the_same_job_never_summarises(stack: Stack) -> None:
    job, steps = run_whole(stack, None)
    entries = stack.requests_for(job)

    assert job.status == "succeeded", job.error
    assert of_kind(steps, StepKind.SUMMARY) == []
    assert all(entry["tool_choice"] is None for entry in entries)
    assert all("tool_choice" not in entry["body"] for entry in entries)
    assert len(entries) == 9


# The pin, and takeovers at a window boundary


@pytest.mark.parametrize(("other", "answers"), [(None, 3), (400, 3), (None, 4), (5000, 4)])
def test_a_worker_with_another_trigger_sends_what_the_first_would_have(
    stack: Stack, other: int | None, answers: int
) -> None:
    """After 3 answers the summary is due; after 4 it is stored."""
    reference, _ = run_whole(stack, TRIGGER)
    job, steps, handed_back = run_split(stack, TRIGGER, other, answers=answers)

    assert handed_back is JobStatus.QUEUED
    assert job.status == "succeeded", job.error
    assert job.takeovers == 1
    assert len(of_kind(steps, StepKind.SUMMARY)) == SUMMARIES
    assert without_job_id(stack.requests_for(job)) == without_job_id(stack.requests_for(reference))


def test_a_job_whose_first_request_named_no_trigger_never_summarises(stack: Stack) -> None:
    job, steps, handed_back = run_split(stack, None, TRIGGER, answers=1)
    entries = stack.requests_for(job)

    assert handed_back is JobStatus.QUEUED
    assert job.status == "succeeded", job.error
    assert of_kind(steps, StepKind.SUMMARY) == []
    assert all(entry["tool_choice"] is None for entry in entries)
    assert {step.payload["summary_trigger"] for step in of_kind(steps, StepKind.MODEL_REQUEST)} == {
        None
    }


def test_a_job_whose_first_request_has_no_trigger_field_never_summarises(stack: Stack) -> None:
    """As a worker built before the field would have left it: a request without an answer."""

    async def body(engine: AsyncEngine, provider: AnthropicProvider) -> tuple[Job, list[Step]]:
        async with engine.begin() as conn:
            await stack.create(conn, SPEC)
            first = await claim_job(conn, worker_id="older-worker")
        assert first is not None
        await held.append_step(
            engine,
            job_id=first.id,
            lease_epoch=first.lease_epoch,
            seq=1,
            kind=StepKind.MODEL_REQUEST,
            payload={"model": "summary", "max_tokens": 1024, "messages": 1},
        )
        async with engine.begin() as conn:
            await expire_lease(conn, first.id)
            taken = await claim_job(conn, worker_id="next-worker")
        assert taken is not None
        await run_job(engine, provider, TOOLS, taken, stack.settings(summary_trigger=TRIGGER))
        return await read(engine, first.id)

    job, steps = stack.run(body)

    assert job.status == "succeeded", job.error
    assert of_kind(steps, StepKind.SUMMARY) == []
    assert all(entry["tool_choice"] is None for entry in stack.requests_for(job))


def test_a_summary_request_left_open_is_sent_again_the_same(stack: Stack) -> None:
    job, steps, handed_back = run_split(stack, TRIGGER, None, in_summary=True)
    first_summary = [e for e in summary_requests(stack.requests_for(job)) if e["summary"] == 1]

    assert handed_back is JobStatus.QUEUED
    assert job.status == "succeeded", job.error
    assert len(of_kind(steps, StepKind.SUMMARY)) == SUMMARIES
    # The first was abandoned when the worker stopped; the second is the same to the byte.
    assert len(first_summary) == 2
    assert first_summary[0]["body_sha256"] == first_summary[1]["body_sha256"]
    # In the log: the abandoned request, then the same request again, with no answer between.
    abandoned, again = of_kind(steps, StepKind.MODEL_REQUEST)[3:5]
    assert abandoned.payload["summary"] is again.payload["summary"] is True
    assert abandoned.lease_epoch < again.lease_epoch
    between = [step.kind for step in steps[abandoned.seq : again.seq - 1]]
    assert StepKind.SUMMARY not in between
    assert StepKind.MODEL_RESPONSE not in between


def test_a_takeover_after_a_summary_step_opens_the_window_from_it(stack: Stack) -> None:
    job, steps, handed_back = run_split(stack, TRIGGER, None, answers=4)
    entries = stack.requests_for(job)

    assert handed_back is JobStatus.QUEUED
    # The first worker stopped right after it stored the summary.
    first_summary = of_kind(steps, StepKind.SUMMARY)[0]
    next_request = of_kind(steps, StepKind.MODEL_REQUEST)[4]
    assert first_summary.seq < next_request.seq
    assert first_summary.lease_epoch < next_request.lease_epoch
    # The next worker's first request is the opening message alone.
    assert entries[4]["body"]["messages"] == [opening(1)]
    assert job.status == "succeeded", job.error
    assert len(of_kind(steps, StepKind.SUMMARY)) == SUMMARIES


# Answers that cannot open a window


@pytest.mark.parametrize(
    ("model", "problem"),
    [
        ("summary-bad-tool-use", "it calls a tool"),
        ("summary-bad-refusal", "it stopped with stop_reason refusal"),
        ("summary-bad-empty", "it has no text"),
        ("summary-bad-whitespace", "it has no text"),
        ("summary-bad-max-tokens", "it stopped with stop_reason max_tokens"),
        ("summary-bad-stop-sequence", "it stopped with stop_reason stop_sequence"),
    ],
)
def test_a_bad_summary_is_stored_with_its_problem_and_fails_the_job(
    stack: Stack, model: str, problem: str
) -> None:
    ran = 0

    def read_file(call: ToolCall) -> ToolResult:
        nonlocal ran
        ran += 1
        return ToolResult(notes(str(call.input["path"])))

    job, steps = run_whole(stack, 100, {**SPEC, "model": model}, stub_tool(read_file))
    (summary,) = of_kind(steps, StepKind.SUMMARY)

    assert job.status == "failed"
    assert job.error == f"the summary is not usable: {problem}"
    assert summary.payload["problem"] == problem
    assert "message" not in summary.payload
    # No tool ran after it, and the summary was asked for once.
    assert [step.kind for step in steps[summary.seq - 1 :]] == [StepKind.SUMMARY, StepKind.ERROR]
    assert ran == 1
    assert len(summary_requests(stack.requests_for(job))) == 1
    if model == "summary-bad-refusal":
        assert summary.payload["refusal"]["category"] == "cyber"
        assert summary.payload["refusal"]["explanation"]


def test_a_window_that_opens_at_or_over_the_trigger_fails_the_job(stack: Stack) -> None:
    job, steps = run_whole(stack, 1)
    opening_response = of_kind(steps, StepKind.MODEL_RESPONSE)[-1]
    size = opening_response.payload["usage"]["input_tokens"]

    assert job.status == "failed"
    assert job.error == (
        f"the summary leaves the next window at or over the trigger: {size} input tokens, trigger 1"
    )
    assert [step.kind for step in steps[-4:]] == [
        StepKind.SUMMARY,
        StepKind.MODEL_REQUEST,
        StepKind.MODEL_RESPONSE,
        StepKind.ERROR,
    ]
    assert re.fullmatch(r"\d+", str(size))


# A halt during an open summary call


async def until_summary_is_open(stack: Stack, job: Job) -> None:
    async with asyncio.timeout(30):
        # The provider's log is read over HTTP: there is no event to wait on.
        while not summary_requests(await asyncio.to_thread(stack.requests_for, job)):  # noqa: ASYNC110
            await asyncio.sleep(0.05)


def test_a_cancel_stops_a_job_in_the_middle_of_a_summary_call(stack: Stack) -> None:
    async def body(
        engine: AsyncEngine, provider: AnthropicProvider
    ) -> tuple[float, Job, list[Step]]:
        async with engine.begin() as conn:
            await stack.create(conn, {**SPEC, "model": "summary-slow"})
            job = await claim_job(conn, worker_id="w1")
        assert job is not None
        run = asyncio.create_task(
            run_job(engine, provider, TOOLS, job, stack.settings(summary_trigger=TRIGGER))
        )
        await until_summary_is_open(stack, job)
        asked = asyncio.get_running_loop().time()
        async with engine.begin() as conn:
            await cancel_job(conn, job.id)
        status = await asyncio.wait_for(run, 30)
        took = asyncio.get_running_loop().time() - asked
        assert status is JobStatus.CANCELLED
        return took, *await read(engine, job.id)

    took, job, steps = stack.run(body)

    assert job.status is JobStatus.CANCELLED
    assert took < WITHIN
    assert of_kind(steps, StepKind.SUMMARY) == []
    assert of_kind(steps, StepKind.MODEL_REQUEST)[-1].payload["summary"] is True


def test_a_lost_lease_ends_an_open_summary_call(stack: Stack) -> None:
    async def body(
        engine: AsyncEngine, provider: AnthropicProvider
    ) -> tuple[JobStatus | None, float, Job | None, Job, list[Step]]:
        async with engine.begin() as conn:
            await stack.create(conn, {**SPEC, "model": "summary-slow"})
            job = await claim_job(conn, worker_id="w1", lease=timedelta(seconds=30))
        assert job is not None
        first = asyncio.create_task(
            run_job(
                engine,
                provider,
                TOOLS,
                job,
                stack.settings(lease=30, heartbeat=0.5, summary_trigger=TRIGGER),
            )
        )
        await until_summary_is_open(stack, job)
        async with engine.begin() as conn:
            await expire_lease(conn, job.id)
            taken = await claim_job(conn, worker_id="w2", lease=timedelta(seconds=30))
        taken_at = asyncio.get_running_loop().time()
        try:
            async with asyncio.timeout(8):
                status = await first
        finally:
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
        dropped_after = asyncio.get_running_loop().time() - taken_at
        return status, dropped_after, taken, *await read(engine, job.id)

    status, dropped_after, taken, job, steps = stack.run(body)

    # Dropped, well inside the 300 s the summary is held back for.
    assert status is None
    assert dropped_after < 8
    assert job == taken
    assert steps[-1].kind is StepKind.MODEL_REQUEST
    assert steps[-1].payload["summary"] is True
