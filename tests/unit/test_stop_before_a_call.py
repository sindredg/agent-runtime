"""A worker told to stop starts no tool call, also not one whose scope it still looks up.

A stop gives the worker the stop grace period, and a sandboxed call that
starts after the stop has to fit in it with its result. So the worker hands
the job back with the call open instead: nothing of the call has happened.
"""

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from runtime.failpoints import Failpoints
from runtime.sandbox.runner import Run, SandboxSettings
from runtime.scope import Network, Resolved, Target
from runtime.store.held import Question
from runtime.store.jobs import Job
from runtime.store.status import JobStatus
from runtime.store.steps import Step, StepKind
from runtime.tools.base import ToolRegistry
from runtime.tools.run_shell import run_shell_tool
from runtime.worker import agent
from runtime.worker.halt import Halt
from runtime.worker.lease import Lease
from runtime.worker.replay import Replay, replay

JOB = uuid.UUID("01a1d655-88bd-7468-afc4-5ca3a1947906")
NOW = datetime.now(UTC)
SCOPE = (Target(host="lab.example"),)


class Writer:
    """Records the steps the worker would write."""

    def __init__(self) -> None:
        self.kinds: list[StepKind] = []

    async def append(self, kind: StepKind, payload: dict[str, Any], *, tokens: int = 0) -> None:
        self.kinds.append(kind)


class Sandbox:
    """Counts the commands it is asked to run."""

    settings = SandboxSettings(image="i", volume="v", guard_image="g", network="n")

    def __init__(self) -> None:
        self.runs = 0

    async def run(
        self, job_id: uuid.UUID, lease_epoch: int, argv: Sequence[str], scope: Any = None
    ) -> Run:
        self.runs += 1
        return Run(b"", 0, 0)

    async def remove_older(self, job_id: uuid.UUID, lease_epoch: int) -> int:
        return 0


def an_open_shell_call() -> Replay:
    response = {
        "id": "msg_1",
        "content": [
            {"type": "tool_use", "id": "toolu_1", "name": "run_shell", "input": {"command": "true"}}
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    return replay(
        "go",
        [
            Step(
                JOB,
                1,
                StepKind.MODEL_REQUEST,
                {"model": "m", "max_tokens": 9, "messages": 1},
                2,
                NOW,
            ),
            Step(JOB, 2, StepKind.MODEL_RESPONSE, response, 2, NOW),
        ],
    )


def held_job() -> Job:
    return Job(
        id=JOB,
        status=JobStatus.RUNNING,
        spec={"prompt": "go"},
        token_budget=1000,
        tokens_used=2,
        deadline=NOW + timedelta(hours=1),
        not_before=None,
        lease_owner="w",
        lease_expires_at=None,
        lease_epoch=2,
        takeovers=0,
        cancel_requested=False,
        error=None,
        created_at=NOW,
    )


type Lookup = Callable[[asyncio.Event], Awaitable[Resolved]]


def next_call(
    monkeypatch: pytest.MonkeyPatch, lookup: Lookup
) -> tuple[Question | None, Writer, Sandbox]:
    """Deal with the open call, with `lookup` in place of the name lookup."""
    writer, sandbox, stop = Writer(), Sandbox(), asyncio.Event()

    async def resolve(targets: Sequence[Target], deny: Sequence[Network]) -> Resolved:
        return await lookup(stop)

    monkeypatch.setattr(agent, "resolve", resolve)

    async def go() -> Question | None:
        return await asyncio.wait_for(
            agent._next_tool_call(
                writer,  # type: ignore[arg-type]
                ToolRegistry([run_shell_tool(sandbox)]),
                held_job(),
                Path("/workspaces") / str(JOB),
                Lease(JOB, 2, timedelta(seconds=30), claimed_at=time.monotonic()),
                an_open_shell_call(),
                Failpoints(),
                stop,
                SCOPE,
                Halt(JOB),
            ),
            timeout=5,
        )

    return asyncio.run(go()), writer, sandbox


RESOLVED = Resolved(allowed=(), hosts=(("lab.example", ("203.0.113.7",)),))


def test_a_stop_during_the_lookup_ends_it_and_starts_no_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def lookup(stop: asyncio.Event) -> Resolved:
        stop.set()
        await asyncio.Event().wait()
        raise AssertionError("never answers")

    asked, writer, sandbox = next_call(monkeypatch, lookup)

    # Not a question for a person: the job goes back with the call open.
    assert asked is None
    assert writer.kinds == []
    assert sandbox.runs == 0


def test_a_stop_that_came_while_the_lookup_answered_starts_no_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def lookup(stop: asyncio.Event) -> Resolved:
        stop.set()
        return RESOLVED

    asked, writer, sandbox = next_call(monkeypatch, lookup)

    assert asked is None
    assert writer.kinds == []
    assert sandbox.runs == 0


def test_without_a_stop_the_call_runs_after_its_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    async def lookup(stop: asyncio.Event) -> Resolved:
        return RESOLVED

    asked, writer, sandbox = next_call(monkeypatch, lookup)

    assert asked is None
    assert writer.kinds == [StepKind.TOOL_INTENT, StepKind.TOOL_RESULT]
    assert sandbox.runs == 1
