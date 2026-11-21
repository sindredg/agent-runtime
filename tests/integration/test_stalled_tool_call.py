"""A request to the tracker that outlives its worker's lease does not become two issues (ADR 0025).

A tool in the worker runs in a thread, and the thread goes on when the
worker loses the job. Its request to the tracker can then still arrive.
Two things keep that from opening a second issue: the request has one
deadline, and the worker that takes the job over waits that long after the
intent before it asks the tracker whether the issue exists.

The tracker here is a stand-in with its own list of issues, which is what
the tests read.
"""

import asyncio
import copy
import json
import threading
import time
from dataclasses import dataclass
from typing import Any, cast

import httpx2
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from runtime.provider.base import ModelRequest, ModelResponse
from runtime.provider.messages import AnthropicProvider
from runtime.store import held
from runtime.store.jobs import Job, claim_job, get_job
from runtime.store.status import JobStatus
from runtime.store.steps import Step, StepKind, read_steps
from runtime.tools.base import Tool, ToolCall, ToolRegistry, ToolResult
from runtime.tools.create_issue import MARGIN, create_issue_tool
from runtime.tools.trackers import FakeTracker
from runtime.worker.agent import run_job

from .conftest import Stack, expire_lease

K = StepKind
URL = "http://tracker.test"
ISSUE = {"title": "CI is red", "body": "test_x failed"}
TURNS: list[list[dict[str, Any]]] = [
    [{"type": "tool_use", "id": "toolu_issue", "name": "create_issue", "input": ISSUE}],
    [{"type": "text", "text": "Reported."}],
]


class Script:
    """A provider that asks for one issue and then ends, and records what it was sent."""

    def __init__(self) -> None:
        self.requests: list[list[dict[str, Any]]] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(copy.deepcopy(request.messages))
        turn = sum(1 for message in request.messages if message["role"] == "assistant")
        content = TURNS[turn]
        calls = any(block["type"] == "tool_use" for block in content)
        return ModelResponse(
            f"msg_{turn}", "m", content, "tool_use" if calls else "end_turn", 10, 5
        )

    async def aclose(self) -> None:
        return None


class StalledTracker:
    """A tracker whose first POST takes `stall` seconds to arrive. It keeps its own list.

    A request that is given up while it stalls never arrives: no issue.
    """

    def __init__(self, stall: float) -> None:
        self.stall = stall
        self.issues: list[dict[str, Any]] = []
        self.posts = 0
        self.given_up = 0
        # The first request is on its way, and later: it arrived or was given up.
        self.on_its_way = threading.Event()
        self.first_ended = threading.Event()

    def __call__(self, request: httpx2.Request) -> Any:
        if request.method == "POST":
            self.posts += 1
            return self.arrive(request, first=self.posts == 1)
        wanted = request.url.params.get("operation_id")
        return httpx2.Response(200, json=[i for i in self.issues if i["operation_id"] == wanted])

    async def arrive(self, request: httpx2.Request, *, first: bool) -> httpx2.Response:
        try:
            if first:
                self.on_its_way.set()
                await asyncio.sleep(self.stall)
        except asyncio.CancelledError:
            self.given_up += 1
            raise
        finally:
            if first:
                self.first_ended.set()
        entry = {"id": len(self.issues) + 1, **json.loads(request.content)}
        self.issues.append(entry)
        return httpx2.Response(201, json=entry)


@dataclass
class Taken:
    job: Job
    steps: list[Step]
    status: JobStatus | None
    requests: list[list[dict[str, Any]]]

    @property
    def kinds(self) -> list[tuple[StepKind, int]]:
        return [(step.kind, step.lease_epoch) for step in self.steps]

    def of(self, kind: StepKind) -> list[Step]:
        return [step for step in self.steps if step.kind is kind]


def stall_then_take_over(stack: Stack, tracker: StalledTracker, create_within: float) -> Taken:
    """The first worker is abandoned while its request is on its way; a second takes over.

    The first worker's thread and its request go on, as they do when a
    worker loses its lease without being killed.
    """
    transport = httpx2.MockTransport(cast(Any, tracker))
    tool = create_issue_tool(FakeTracker(URL), transport=transport, create_within=create_within)
    tools = ToolRegistry([tool])
    script = Script()

    async def body(engine: AsyncEngine, _: AnthropicProvider) -> Taken:
        async with engine.begin() as conn:
            await stack.create(conn, {"prompt": "report the failure"})
            first_claim = await claim_job(conn, worker_id="w1")
        assert first_claim is not None
        first = asyncio.create_task(run_job(engine, script, tools, first_claim, stack.settings()))
        assert await asyncio.to_thread(tracker.on_its_way.wait, 30)
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)

        async with engine.begin() as conn:
            await expire_lease(conn, first_claim.id)
            second_claim = await claim_job(conn, worker_id="w2")
        assert second_claim is not None
        assert (second_claim.lease_epoch, second_claim.takeovers) == (2, 1)
        status = await run_job(engine, script, tools, second_claim, stack.settings())
        # The first worker's request has arrived or was given up by now. Wait
        # for it all the same: the tracker's list is read only after that.
        assert await asyncio.to_thread(tracker.first_ended.wait, 30)
        async with engine.begin() as conn:
            job = await get_job(conn, first_claim.id)
            assert job is not None
            steps = await read_steps(conn, job.id)
        return Taken(job, steps, status, script.requests)

    return stack.run(body)


def test_a_request_that_arrives_after_the_takeover_is_found_and_not_sent_again(
    stack: Stack,
) -> None:
    # The request arrives 1.5 s after it was sent: after the takeover, inside its deadline.
    tracker = StalledTracker(stall=1.5)
    taken = stall_then_take_over(stack, tracker, create_within=3.0)

    # The tracker's own list: one issue for the one call.
    assert len(tracker.issues) == 1
    assert tracker.posts == 1
    assert taken.status is JobStatus.SUCCEEDED
    assert taken.kinds[:4] == [
        (K.MODEL_REQUEST, 1),
        (K.MODEL_RESPONSE, 1),
        (K.TOOL_INTENT, 1),
        (K.TOOL_RESULT, 2),
    ]
    intent, result = taken.steps[2], taken.steps[3]
    bound = 3.0 + MARGIN
    assert intent.payload["ends_within"] == bound
    # The second worker asked the tracker only after the bound had passed
    # since the intent, by the database clock, and took the issue it found.
    assert (result.created_at - intent.created_at).total_seconds() >= bound
    assert result.payload == {
        "tool_use_id": "toolu_issue",
        "content": "created issue 1: CI is red",
        "is_error": False,
        "recovered": True,
    }


def test_a_request_given_up_at_its_deadline_is_sent_again_once(stack: Stack) -> None:
    # The request would arrive after a minute. Its deadline is 1 s.
    tracker = StalledTracker(stall=60.0)
    taken = stall_then_take_over(stack, tracker, create_within=1.0)

    assert tracker.given_up == 1
    # The second worker's request is the only one that arrived.
    assert tracker.posts == 2
    assert len(tracker.issues) == 1
    assert taken.status is JobStatus.SUCCEEDED
    assert [step.lease_epoch for step in taken.of(K.TOOL_INTENT)] == [1, 2]
    (result,) = taken.of(K.TOOL_RESULT)
    assert result.lease_epoch == 2
    assert "recovered" not in result.payload
    first_intent, second_intent = taken.of(K.TOOL_INTENT)
    # Not asked, and so not sent again, before the bound had passed.
    waited = (second_intent.created_at - first_intent.created_at).total_seconds()
    assert waited >= 1.0 + MARGIN


def test_the_tools_deadline_counts_from_before_its_intent_is_written(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The takeover's wait counts from the intent. A slow write of it must not buy the call time."""
    write = held.append_step

    async def slow_for_an_intent(engine: AsyncEngine, **step: Any) -> Step:
        if step["kind"] is K.TOOL_INTENT:
            await asyncio.sleep(0.5)
        return await write(engine, **step)

    monkeypatch.setattr(held, "append_step", slow_for_an_intent)
    behind: list[float] = []

    def run(call: ToolCall) -> ToolResult:
        assert call.started is not None
        behind.append(time.monotonic() - call.started)
        return ToolResult("created issue 1: CI is red")

    schema = {"type": "object"}
    tools = ToolRegistry([Tool("create_issue", "Open an issue.", schema, run, ends_within=12.0)])
    script = Script()

    async def body(engine: AsyncEngine, _: AnthropicProvider) -> JobStatus | None:
        async with engine.begin() as conn:
            await stack.create(conn, {"prompt": "report the failure"})
            claimed = await claim_job(conn, worker_id="w1")
        assert claimed is not None
        return await run_job(engine, script, tools, claimed, stack.settings())

    assert stack.run(body) is JobStatus.SUCCEEDED
    # The tool sees how long ago its call began: the write of the intent is in it.
    assert len(behind) == 1
    assert behind[0] >= 0.5
