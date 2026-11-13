"""The agent loop against real Postgres and the fake provider over HTTP.

The fake provider's request log is the independent record of what the
worker sent. The step log is never checked against itself alone.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from runtime.provider.base import ModelRequest, ModelResponse
from runtime.provider.messages import AnthropicProvider
from runtime.store.engine import create_engine
from runtime.store.jobs import Job, claim_job, create_job, get_job
from runtime.store.status import JobStatus
from runtime.store.steps import Step, StepKind, read_steps
from runtime.tools import default_tools
from runtime.tools.base import Tool, ToolRegistry, ToolResult
from runtime.worker.agent import AgentSettings, run_job
from runtime.worker.loop import work

K = StepKind
HELLO = {"prompt": "Read hello.txt and say what it holds.", "model": "hello"}


def stub_tool(run: Callable[[Path, dict[str, Any]], ToolResult]) -> ToolRegistry:
    """A registry whose `read_file` is the given function."""
    schema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    return ToolRegistry([Tool("read_file", "Read a file.", schema, run)])


FIXED = stub_tool(lambda workspace, tool_input: ToolResult(f"content of {tool_input['path']}"))


@dataclass
class Outcome:
    job: Job
    steps: list[Step]
    # The fake provider's log of the requests that carried this job's id.
    requests: list[dict[str, Any]]

    @property
    def kinds(self) -> list[StepKind]:
        return [step.kind for step in self.steps]


@dataclass
class Stack:
    database_url: str
    fake_provider_url: str
    workspace_root: Path

    def settings(self, default_model: str = "hello") -> AgentSettings:
        return AgentSettings(
            default_model=default_model, max_tokens=1024, workspace_root=self.workspace_root
        )

    def run[T](self, body: Callable[[AsyncEngine, AnthropicProvider], Awaitable[T]]) -> T:
        async def go() -> T:
            engine = create_engine(self.database_url)
            provider = AnthropicProvider(api_key="test", base_url=self.fake_provider_url)
            try:
                return await body(engine, provider)
            finally:
                await provider.aclose()
                await engine.dispose()

        return asyncio.run(go())

    def requests_for(self, job: Job) -> list[dict[str, Any]]:
        log: list[dict[str, Any]] = httpx2.get(
            f"{self.fake_provider_url}/_fake/requests", params={"user_id": str(job.id)}, timeout=10
        ).json()
        return log

    def run_one(
        self, spec: dict[str, Any], tools: ToolRegistry = FIXED, default_model: str = "hello"
    ) -> Outcome:
        """Create one job, claim it, run it, and read back everything about it."""

        async def body(engine: AsyncEngine, provider: AnthropicProvider) -> tuple[Job, list[Step]]:
            async with engine.begin() as conn:
                await create_job(
                    conn,
                    spec=spec,
                    token_budget=100_000,
                    deadline=datetime.now(UTC) + timedelta(hours=1),
                )
                claimed = await claim_job(conn, worker_id="test-worker")
            assert claimed is not None
            await run_job(engine, provider, tools, claimed, self.settings(default_model))
            async with engine.begin() as conn:
                job = await get_job(conn, claimed.id)
                assert job is not None
                return job, await read_steps(conn, job.id)

        job, steps = self.run(body)
        return Outcome(job, steps, self.requests_for(job))


@pytest.fixture
def stack(database_url: str, fake_provider_url: str, tmp_path: Path) -> Stack:
    return Stack(database_url, fake_provider_url, tmp_path / "workspaces")


def test_scripted_job_produces_the_expected_step_sequence(stack: Stack) -> None:
    outcome = stack.run_one({**HELLO, "system": "Be brief."})
    job, steps = outcome.job, outcome.steps

    assert outcome.kinds == [
        K.MODEL_REQUEST,
        K.MODEL_RESPONSE,
        K.TOOL_INTENT,
        K.TOOL_RESULT,
        K.MODEL_REQUEST,
        K.MODEL_RESPONSE,
    ]
    assert [step.seq for step in steps] == [1, 2, 3, 4, 5, 6]
    assert {step.lease_epoch for step in steps} == {1}

    call = steps[1].payload["content"][-1]
    assert steps[1].payload["stop_reason"] == "tool_use"
    assert steps[2].payload == {
        "tool_use_id": call["id"],
        "name": "read_file",
        "input": {"path": "hello.txt"},
    }
    assert steps[3].payload == {
        "tool_use_id": call["id"],
        "content": "content of hello.txt",
        "is_error": False,
    }
    assert steps[5].payload["stop_reason"] == "end_turn"

    assert job.status is JobStatus.SUCCEEDED
    assert job.error is None
    assert job.lease_owner is None
    usage = [step.payload["usage"] for step in steps if step.kind is K.MODEL_RESPONSE]
    assert job.tokens_used == sum(u["input_tokens"] + u["output_tokens"] for u in usage) > 0

    # What the provider really received: two requests, and the second one
    # carries the tool's output.
    assert [(r["status"], r["turn"]) for r in outcome.requests] == [(200, 0), (200, 1)]
    first, second = (r["body"] for r in outcome.requests)
    assert first["model"] == "hello"
    assert first["system"] == "Be brief."
    assert first["max_tokens"] == 1024
    assert [tool["name"] for tool in first["tools"]] == ["read_file"]
    assert first["messages"] == [{"role": "user", "content": HELLO["prompt"]}]
    assert second["messages"][1] == {"role": "assistant", "content": steps[1].payload["content"]}
    assert second["messages"][2] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": call["id"],
                "content": "content of hello.txt",
                "is_error": False,
            }
        ],
    }
    assert steps[0].payload == {"model": "hello", "max_tokens": 1024, "messages": 1}
    assert steps[4].payload == {"model": "hello", "max_tokens": 1024, "messages": 3}


def test_the_configured_model_is_used_when_the_spec_names_none(stack: Stack) -> None:
    outcome = stack.run_one({"prompt": "go"}, default_model="hello")
    assert outcome.job.status is JobStatus.SUCCEEDED
    assert {r["body"]["model"] for r in outcome.requests} == {"hello"}


def boom(workspace: Path, tool_input: dict[str, Any]) -> ToolResult:
    raise RuntimeError("disk on fire")


@pytest.mark.parametrize(
    ("tools", "expected"),
    [
        (ToolRegistry(), "unknown tool: read_file"),
        (stub_tool(boom), "the tool failed: disk on fire"),
        (stub_tool(lambda w, i: ToolResult("a\x00b")), "the tool's output cannot be stored"),
        (stub_tool(lambda w, i: ToolResult("no such file", is_error=True)), "no such file"),
    ],
    ids=["unknown-tool", "tool-raises", "output-not-storable", "tool-reports-error"],
)
def test_a_failed_tool_call_is_an_error_result_and_the_job_goes_on(
    stack: Stack, tools: ToolRegistry, expected: str
) -> None:
    outcome = stack.run_one(HELLO, tools)
    result = outcome.steps[3]
    assert result.kind is K.TOOL_RESULT
    assert result.payload["is_error"] is True
    assert expected in result.payload["content"]
    assert outcome.job.status is JobStatus.SUCCEEDED
    assert len(outcome.steps) == 6
    # The model was told about the failure.
    sent = outcome.requests[1]["body"]["messages"][2]["content"][0]
    assert sent["is_error"] is True
    assert expected in sent["content"]


def test_a_provider_error_fails_the_job_with_an_error_step(stack: Stack) -> None:
    outcome = stack.run_one({"prompt": "go", "model": "no-such-scenario"})
    assert outcome.kinds == [K.MODEL_REQUEST, K.ERROR]
    assert outcome.job.status is JobStatus.FAILED
    assert outcome.job.error is not None
    assert "404" in outcome.job.error
    assert outcome.steps[1].payload == {"message": outcome.job.error}
    assert outcome.job.lease_owner is None
    assert outcome.job.tokens_used == 0
    assert [r["status"] for r in outcome.requests] == [404]


@pytest.mark.parametrize(
    "spec",
    [{"task": "no prompt"}, {"prompt": ""}, {"prompt": "go", "files": {"../escape.txt": "x"}}],
)
def test_a_spec_the_worker_cannot_use_fails_the_job_before_any_model_call(
    stack: Stack, spec: dict[str, Any]
) -> None:
    outcome = stack.run_one(spec)
    assert outcome.kinds == [K.ERROR]
    assert outcome.job.status is JobStatus.FAILED
    assert outcome.requests == []
    assert not (stack.workspace_root.parent / "escape.txt").exists()


class OneResponse:
    """A provider that gives one fixed response, for stop reasons no scenario scripts."""

    def __init__(self, response: ModelResponse) -> None:
        self.response = response
        self.calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        return self.response

    async def aclose(self) -> None:
        return None


@pytest.mark.parametrize(
    ("stop_reason", "error"),
    [
        ("max_tokens", "cut off at max_tokens"),
        ("refusal", "declined"),
        ("pause_turn", "unexpected stop_reason: pause_turn"),
    ],
)
def test_a_response_that_did_not_finish_fails_the_job_and_runs_no_tool(
    stack: Stack, stop_reason: str, error: str
) -> None:
    """The response holds a tool call, but its stop reason says it is not to be trusted."""
    ran: list[str] = []

    def record(workspace: Path, tool_input: dict[str, Any]) -> ToolResult:
        ran.append("ran")
        return ToolResult("ok")

    call = {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "a"}}
    provider = OneResponse(ModelResponse("msg_1", "m", [call], stop_reason, 5, 7))

    async def body(engine: AsyncEngine, _: AnthropicProvider) -> tuple[Job | None, list[Step]]:
        async with engine.begin() as conn:
            await create_job(
                conn,
                spec={"prompt": "go"},
                token_budget=1000,
                deadline=datetime.now(UTC) + timedelta(hours=1),
            )
            claimed = await claim_job(conn, worker_id="test-worker")
        assert claimed is not None
        await run_job(engine, provider, stub_tool(record), claimed, stack.settings())
        async with engine.begin() as conn:
            return await get_job(conn, claimed.id), await read_steps(conn, claimed.id)

    job, steps = stack.run(body)
    assert job is not None
    assert [step.kind for step in steps] == [K.MODEL_REQUEST, K.MODEL_RESPONSE, K.ERROR]
    assert job.status is JobStatus.FAILED
    assert error in (job.error or "")
    assert ran == []
    assert provider.calls == 1
    assert job.tokens_used == 12


def test_spec_files_are_written_to_the_job_workspace_before_tools_run(stack: Stack) -> None:
    seen: list[str] = []

    def read(workspace: Path, tool_input: dict[str, Any]) -> ToolResult:
        seen.append((workspace / tool_input["path"]).read_text())
        return ToolResult("ok")

    outcome = stack.run_one(
        {**HELLO, "files": {"hello.txt": "Hi!\n", "sub/b.txt": "b"}}, stub_tool(read)
    )
    assert seen == ["Hi!\n"]
    workspace = stack.workspace_root / str(outcome.job.id)
    assert (workspace / "sub" / "b.txt").read_text() == "b"


def test_read_file_returns_the_seeded_file_to_the_model(stack: Stack) -> None:
    """The real tool: the file from the spec reaches the provider as the tool result."""
    text = "Hello from the workspace.\n"
    outcome = stack.run_one({**HELLO, "files": {"hello.txt": text}}, default_tools())
    assert outcome.job.status is JobStatus.SUCCEEDED
    assert outcome.steps[3].payload["content"] == text
    assert outcome.steps[3].payload["is_error"] is False
    sent = outcome.requests[1]["body"]["messages"][2]["content"][0]
    assert sent["content"] == text
    assert outcome.requests[0]["body"]["tools"] == list(default_tools().definitions())


def test_read_file_of_a_missing_file_is_an_error_result(stack: Stack) -> None:
    outcome = stack.run_one(HELLO, default_tools())
    assert outcome.job.status is JobStatus.SUCCEEDED
    assert outcome.steps[3].payload == {
        "tool_use_id": outcome.steps[2].payload["tool_use_id"],
        "content": "no such file: hello.txt",
        "is_error": True,
    }


def test_work_loop_runs_queued_jobs_oldest_first_and_stops_when_told(stack: Stack) -> None:
    async def body(engine: AsyncEngine, provider: AnthropicProvider) -> list[Job]:
        created = []
        for _ in range(3):
            async with engine.begin() as conn:
                created.append(
                    await create_job(
                        conn,
                        spec=HELLO,
                        token_budget=100_000,
                        deadline=datetime.now(UTC) + timedelta(hours=1),
                    )
                )
        stop = asyncio.Event()
        worker = asyncio.create_task(
            work(
                engine,
                provider,
                FIXED,
                stack.settings(),
                worker_id="loop-worker",
                poll_interval=0.05,
                stop=stop,
            )
        )
        async with asyncio.timeout(30):
            while True:
                async with engine.begin() as conn:
                    jobs = [await get_job(conn, job.id) for job in created]
                if all(job is not None and job.status is JobStatus.SUCCEEDED for job in jobs):
                    break
                await asyncio.sleep(0.05)
            # With nothing left to claim the loop is polling. It ends when told.
            stop.set()
            await worker
        return [job for job in jobs if job is not None]

    jobs = stack.run(body)
    assert len(jobs) == 3
    # Each job's first request reached the provider before the next job's.
    first_seen = [stack.requests_for(job)[0]["seq"] for job in jobs]
    assert first_seen == sorted(first_seen)
    assert all(job.lease_epoch == 1 for job in jobs)
