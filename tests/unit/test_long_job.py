"""A job longer than the fake provider's context window, carried by summaries.

The worker loop runs in process against the fake provider's app, the fake
tracker's app and a step log in memory, as in `test_summary.py`. No database
and no stack. Sizes are the fake's own estimate: a quarter of the characters
of the request's `system`, `tools` and `messages` as JSON.
"""

import asyncio
import json
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any

import httpx2
from fastapi.testclient import TestClient

from fake_provider.app import create_app
from fake_provider.scenario import Scenario, ToolUseBlock, load_scenarios
from fake_tracker.app import create_app as create_tracker
from runtime.provider.messages import AnthropicProvider
from runtime.spec import JobSpec
from runtime.store.jobs import Job
from runtime.store.status import JobStatus
from runtime.store.steps import Step, StepKind
from runtime.tools.base import ToolRegistry
from runtime.tools.create_issue import create_issue_tool
from runtime.tools.read_file import READ_FILE
from runtime.tools.trackers import FakeTracker
from runtime.worker import agent
from runtime.worker.halt import Halt
from runtime.worker.lease import Lease

JOB = uuid.UUID("01a1d655-88bd-7468-afc4-5ca3a1947908")
NOW = datetime.now(UTC)
LOCAL = "http://localhost"
NAME = "long-window"
SPEC = {
    "prompt": "Read the notes of every part in order, open an issue where they ask for one,"
    " then say you are done.",
    "model": NAME,
}
# The trigger the scenario is sized for, and the summaries it gives.
TRIGGER = 3000
SUMMARIES = 3
# The long stack's first issue number, and one with a digit more.
FIRST_ISSUE = "100000"
WIDER = "1000000"
REFUSAL = re.compile(r"prompt is too long: (\d+) tokens > (\d+) maximum")
# The turn whose response the scenario sizes to one token under the trigger.
BOUNDARY = 10
# A short `system` for the job. The scenario is sized for a job without one.
SYSTEM = "Keep every note."


def scenario(window: bool = True) -> Scenario:
    """The packaged scenario, without its window if asked."""
    with resources.as_file(resources.files("fake_provider") / "scenarios") as packaged:
        found = load_scenarios(packaged)[NAME]
    return found if window else found.model_copy(update={"context_window": None})


def window() -> int:
    size = scenario().context_window
    assert size is not None
    return size


def seed(workspace: Path) -> None:
    """The files the scenario reads, each a short line."""
    for turn in scenario().turns:
        for block in turn.content:
            if isinstance(block, ToolUseBlock) and block.name == "read_file":
                path = workspace / str(block.input["path"])
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"{block.input['path']}: read.\n")


class Writer:
    """The step log in memory, folded into the state as the worker's own writer does."""

    def __init__(self) -> None:
        self.steps: list[Step] = []
        self.tokens_added = 0
        self.state: Any = None

    async def append(self, kind: StepKind, payload: dict[str, Any], *, tokens: int = 0) -> Step:
        written = Step(JOB, len(self.steps) + 1, kind, json.loads(json.dumps(payload)), 1, NOW)
        self.steps.append(written)
        self.tokens_added += tokens
        if self.state is not None:
            self.state.apply(written)
        return written


def tracker_transport(first_issue: str) -> httpx2.MockTransport:
    """The fake tracker's app, numbering from `first_issue`, behind the tool's transport."""
    tracker = TestClient(create_tracker({"FAKE_TRACKER_FIRST_ISSUE": first_issue}), base_url=LOCAL)

    def forward(request: httpx2.Request) -> httpx2.Response:
        answer = tracker.request(
            request.method, str(request.url), content=request.content, headers=request.headers
        )
        return httpx2.Response(answer.status_code, content=answer.content)

    return httpx2.MockTransport(forward)


def job(spec: dict[str, Any]) -> Job:
    return Job(
        id=JOB,
        status=JobStatus.RUNNING,
        spec=spec,
        token_budget=1_000_000,
        tokens_used=0,
        deadline=NOW + timedelta(hours=1),
        not_before=None,
        lease_owner="w",
        lease_expires_at=None,
        lease_epoch=1,
        takeovers=0,
        cancel_requested=False,
        error=None,
        created_at=NOW,
    )


def run(
    workspace: Path,
    trigger: int | None,
    *,
    with_window: bool = True,
    first_issue: str = FIRST_ISSUE,
    system: str | None = None,
) -> tuple[str, Writer, list[dict[str, Any]]]:
    """Run the job to its end. Returns (outcome, step log, the fake's request log).

    `system` is the spec's `system`. Left out, the job has none.
    """
    seed(workspace)
    spec = SPEC if system is None else {**SPEC, "system": system}
    app = create_app({NAME: scenario(with_window)})
    # The tools of a worker with a tracker and no sandbox, in its order.
    tools = ToolRegistry(
        [READ_FILE, create_issue_tool(FakeTracker(LOCAL), transport=tracker_transport(first_issue))]
    )
    settings = agent.AgentSettings(
        default_model=NAME,
        max_tokens=32_000,
        workspace_root=workspace.parent,
        summary_trigger=trigger,
    )
    writer = Writer()

    async def go() -> str:
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app)) as http:
            provider = AnthropicProvider(api_key="k", base_url=LOCAL, http_client=http)
            try:
                status, _ = await asyncio.wait_for(
                    agent._loop(
                        writer,  # type: ignore[arg-type]
                        provider,
                        tools,
                        job(spec),
                        JobSpec.model_validate(spec),
                        settings,
                        workspace,
                        Lease(JOB, 1, timedelta(seconds=60), claimed_at=time.monotonic()),
                        [],
                        asyncio.Event(),
                        Halt(JOB),
                    ),
                    timeout=60,
                )
                return status.value
            except agent._JobFailed as failed:
                return f"failed: {failed}"
            finally:
                await provider.aclose()

    outcome = asyncio.run(go())
    return outcome, writer, asyncio.run(_log(app))


async def _log(app: Any) -> list[dict[str, Any]]:
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=LOCAL) as http:
        found: list[dict[str, Any]] = (await http.get("/_fake/requests")).json()
        return found


def tokens(entry: dict[str, Any]) -> int:
    """Input plus output tokens of an answered request: what the trigger is compared with."""
    return int(entry["usage"]["input_tokens"] + entry["usage"]["output_tokens"])


def kinds(writer: Writer, kind: StepKind) -> list[Step]:
    return [s for s in writer.steps if s.kind is kind]


def first_summary(log: list[dict[str, Any]]) -> dict[str, Any]:
    return next(entry for entry in log if entry["tool_choice"] is not None)


# The scenario's size


def test_the_whole_conversation_is_at_least_three_windows(tmp_path: Path) -> None:
    """With no window and no trigger, the last request holds the whole job."""
    outcome, _, log = run(tmp_path / "w", None, with_window=False)

    assert outcome == "succeeded"
    assert [entry["turn"] for entry in log] == list(range(len(scenario().turns)))
    assert log[-1]["input_estimate"] >= 3 * window()


# The window, with the trigger off and on


def test_with_no_trigger_a_request_over_the_window_is_refused(tmp_path: Path) -> None:
    outcome, writer, log = run(tmp_path / "w", None)
    *answered, refused = log
    turn = sum(1 for m in refused["body"]["messages"] if m["role"] == "assistant")

    assert refused["status"] == 400
    match = REFUSAL.fullmatch(refused["error"])
    assert match is not None
    assert (int(match[1]), int(match[2])) == (refused["input_estimate"], window())
    assert refused["input_estimate"] > window()
    # A turn the script holds: the window stops the job, not the end of the script.
    assert turn < len(scenario().turns)
    assert all(e["status"] == 200 and e["input_estimate"] <= window() for e in answered)
    assert outcome.startswith("failed: model call failed: provider answered 400")
    assert "prompt is too long" in outcome
    assert kinds(writer, StepKind.SUMMARY) == []


def test_with_the_trigger_the_job_summarises_three_times_and_fits_the_window(
    tmp_path: Path,
) -> None:
    outcome, writer, log = run(tmp_path / "w", TRIGGER)
    summaries = [entry for entry in log if entry["tool_choice"] is not None]

    assert outcome == "succeeded"
    assert len(kinds(writer, StepKind.SUMMARY)) == SUMMARIES
    assert [entry["summary"] for entry in summaries] == [1, 2, 3]
    # Every request, summary requests included, is answered and inside the window.
    assert all(entry["status"] == 200 for entry in log)
    assert max(entry["input_estimate"] for entry in log) <= window()
    assert all(entry["input_estimate"] <= window() for entry in summaries)
    # Every turn of the script was answered once, in order, across the windows.
    turns = [entry["turn"] for entry in log if entry["tool_choice"] is None]
    assert turns == list(range(len(scenario().turns)))


def test_no_id_repeats_across_the_windows(tmp_path: Path) -> None:
    """The fake builds ids from the job-wide turn its marker names, not the window's turn."""
    outcome, writer, log = run(tmp_path / "w", TRIGGER)
    message_ids = [entry["response_id"] for entry in log]
    tool_ids = [
        block["id"]
        for step in kinds(writer, StepKind.MODEL_RESPONSE)
        for block in step.payload["content"]
        if block["type"] == "tool_use"
    ]

    assert len(message_ids) == len(set(message_ids))
    assert len(tool_ids) == len(set(tool_ids))
    assert len(tool_ids) == len(scenario().turns) - 1
    assert outcome == "succeeded"


# Tracker numbers and the summary boundary


def test_the_issue_numbers_of_the_long_stack_have_one_width(tmp_path: Path) -> None:
    _, writer, _ = run(tmp_path / "w", TRIGGER)
    created = [
        s.payload["content"]
        for s in kinds(writer, StepKind.TOOL_RESULT)
        if str(s.payload["content"]).startswith("created issue ")
    ]
    numbers = [re.match(r"created issue (\d+):", text) for text in created]

    assert len(numbers) >= SUMMARIES + 1
    assert [int(n[1]) for n in numbers if n] == list(range(100_000, 100_000 + len(created)))


def issue_results(entry: dict[str, Any]) -> int:
    """The `create_issue` results the request holds."""
    return json.dumps(entry["body"]["messages"]).count("created issue ")


def test_one_more_digit_in_an_issue_number_moves_the_summary(tmp_path: Path) -> None:
    """At the trigger, one response is exactly T - 1 tokens with six-digit issue numbers.

    Its request holds one issue result, the first. The fake counts a quarter
    of the characters, and the scenario puts that request one character short
    of a multiple of four, so a seventh digit takes the response to T. The
    summary then comes right after it, a turn earlier.
    """
    outcome, _, narrow = run(tmp_path / "narrow", TRIGGER)
    wider, _, wide = run(tmp_path / "wide", TRIGGER, first_issue=WIDER)

    assert (outcome, wider) == ("succeeded", "succeeded")
    assert issue_results(narrow[BOUNDARY]) == 1
    assert tokens(narrow[BOUNDARY]) == TRIGGER - 1
    assert tokens(wide[BOUNDARY]) == TRIGGER
    assert first_summary(wide)["turn"] == BOUNDARY + 1
    # Without the seventh digit the next response reaches the trigger instead.
    assert first_summary(narrow)["turn"] == BOUNDARY + 2


# The spec's `system`


def test_every_request_of_a_summarising_job_carries_the_specs_system(tmp_path: Path) -> None:
    """The `system` of the spec is in every request the job sends.

    That holds for a summary request and for the first request of the
    window after it, which carries nothing else of the conversation before.
    """
    outcome, writer, log = run(tmp_path / "w", TRIGGER, system=SYSTEM)
    summaries = [index for index, entry in enumerate(log) if entry["tool_choice"] is not None]
    openers = [log[index + 1] for index in summaries if index + 1 < len(log)]

    assert outcome == "succeeded"
    assert len(kinds(writer, StepKind.SUMMARY)) >= 1
    # The log holds a summary request, and a request that opens a window
    # from a summary: one message.
    assert len(summaries) >= 1
    assert len(openers) >= 1
    assert all(len(entry["body"]["messages"]) == 1 for entry in openers)
    assert [entry["body"].get("system") for entry in log] == [SYSTEM] * len(log)
