"""The summary step (ADR 0033): its texts, its replay, the pin, and the loop that makes it.

The loop runs here against the fake provider in process and a step log in
memory, so the rules are checked without a database. The same jobs run
against Postgres and the stack's fake in `tests/integration/test_summary.py`.
A job with a trigger also asks for one tool call a response, so the trigger
is checked after each call's result.
"""

import asyncio
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any

import httpx2
import pytest

from fake_provider.app import create_app
from fake_provider.scenario import Scenario, load_scenarios
from runtime.provider.base import ModelRequest, ModelResponse
from runtime.provider.messages import AnthropicProvider
from runtime.spec import JobSpec
from runtime.store.held import LeaseLost
from runtime.store.jobs import Job
from runtime.store.status import JobStatus
from runtime.store.steps import Step, StepKind
from runtime.tools.base import Tool, ToolCall, ToolRegistry, ToolResult
from runtime.worker import agent
from runtime.worker.budget import ALLOWANCE, input_bound
from runtime.worker.halt import Halt, Halted, HaltReason
from runtime.worker.lease import Lease
from runtime.worker.replay import replay
from runtime.worker.summary import (
    CONTINUATION,
    SUMMARY_INSTRUCTION,
    opening_message,
    summary_messages,
    summary_problem,
    summary_text,
)

JOB = uuid.UUID("01a1d655-88bd-7468-afc4-5ca3a1947907")
NOW = datetime.now(UTC)
LOCAL = "http://localhost"
SPEC = {
    "prompt": "Read the notes files a.txt to h.txt in order, then say you are done.",
    "model": "summary",
}
# The `summary` scenario's summary text, held by the test.
SUMMARY_TEXT = (
    "The task: read the notes files a.txt to h.txt in order, then say done."
    " Read so far: every file before the turn named below."
)
# Fires twice in the `summary` scenario with the notes below: after the
# third and the sixth read. Measured by the fake's own count.
TRIGGER = 1000
SUMMARIES = 2
# The `tool_choice` of a summary request, and of every other request of a
# job with a trigger, as the API takes them. Held by the test.
NONE = {"type": "none"}
ONE_CALL = {"type": "auto", "disable_parallel_tool_use": True}


# The texts, word for word as ADR 0033 freezes them


def test_the_instruction_and_the_continuation_line_are_the_frozen_texts() -> None:
    assert SUMMARY_INSTRUCTION == (
        "Your earlier messages are about to be removed to make room. Write a summary that lets"
        " you continue this task with nothing else: the task and every instruction you were"
        " given, the facts you found that later steps need, what you have done, and what is"
        " left to do. Plain text. Do not call a tool."
    )
    assert CONTINUATION == (
        "That was your own summary of this job so far, written before your earlier messages"
        " were removed. Continue the task from where it stops."
    )


def test_the_adr_quotes_both_texts() -> None:
    adr = Path(__file__).resolve().parents[2] / "docs" / "adr"
    (record,) = adr.glob("0033-*.md")
    # Quoted as Markdown block quotes, wrapped at any space.
    lines = (line.removeprefix(">").strip() for line in record.read_text().splitlines())
    text = " ".join(" ".join(lines).split())
    assert SUMMARY_INSTRUCTION in text
    assert CONTINUATION in text


# The request and the answer


def result(tool_use_id: str, content: str = "ok") -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content,
        "is_error": False,
    }


def test_the_instruction_follows_the_last_tool_result_and_the_conversation_is_kept() -> None:
    conversation: list[dict[str, Any]] = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "x"}]},
        {"role": "user", "content": [result("t1")]},
    ]
    before = json.dumps(conversation)

    sent = summary_messages(conversation)

    assert sent[:2] == conversation[:2]
    assert sent[-1] == {
        "role": "user",
        "content": [result("t1"), {"type": "text", "text": SUMMARY_INSTRUCTION}],
    }
    assert json.dumps(conversation) == before


def test_the_summary_text_is_the_text_blocks_joined_with_a_blank_line() -> None:
    content = [
        {"type": "thinking", "thinking": "private", "signature": "s"},
        {"type": "text", "text": "one"},
        {"type": "text", "text": "two"},
    ]
    assert summary_text(content) == "one\n\ntwo"
    assert opening_message("one\n\ntwo") == {
        "role": "user",
        "content": [{"type": "text", "text": f"one\n\ntwo\n\n{CONTINUATION}"}],
    }


TEXT = [{"type": "text", "text": "the summary"}]


@pytest.mark.parametrize(
    ("stop_reason", "content", "problem"),
    [
        ("end_turn", TEXT, None),
        ("tool_use", [*TEXT, {"type": "tool_use", "id": "t", "name": "x", "input": {}}], "tool"),
        ("end_turn", [*TEXT, {"type": "tool_use", "id": "t", "name": "x", "input": {}}], "tool"),
        ("refusal", TEXT, "refusal"),
        ("max_tokens", TEXT, "max_tokens"),
        ("stop_sequence", TEXT, "stop_sequence"),
        ("end_turn", [{"type": "text", "text": ""}], "no text"),
        ("end_turn", [{"type": "text", "text": " \n\t"}], "no text"),
        ("end_turn", [], "no text"),
    ],
)
def test_a_summary_is_usable_only_with_end_turn_text_and_no_tool_call(
    stop_reason: str, content: list[dict[str, Any]], problem: str | None
) -> None:
    found = summary_problem(stop_reason, content)
    if problem is None:
        assert found is None
    else:
        assert found is not None
        assert problem in found


# Replay


def step(seq: int, kind: StepKind, payload: dict[str, Any]) -> Step:
    return Step(JOB, seq, kind, payload, 1, NOW)


def request_step(seq: int, **extra: Any) -> Step:
    return step(
        seq, StepKind.MODEL_REQUEST, {"model": "m", "max_tokens": 9, "messages": 1, **extra}
    )


def response_step(seq: int, *content: dict[str, Any], tokens: tuple[int, int] = (5, 5)) -> Step:
    calls = any(block["type"] == "tool_use" for block in content)
    return step(
        seq,
        StepKind.MODEL_RESPONSE,
        {
            "id": f"msg_{seq}",
            "content": list(content),
            "stop_reason": "tool_use" if calls else "end_turn",
            "usage": {"input_tokens": tokens[0], "output_tokens": tokens[1]},
        },
    )


def call(tool_use_id: str) -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_use_id, "name": "read_file", "input": {"path": "a"}}


def result_step(seq: int, tool_use_id: str) -> Step:
    return step(
        seq,
        StepKind.TOOL_RESULT,
        {"tool_use_id": tool_use_id, "content": "notes", "is_error": False},
    )


OPENING = opening_message("the summary")


def summary_step(seq: int, **changes: Any) -> Step:
    payload = {
        "id": f"msg_{seq}",
        "content": TEXT,
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 40, "output_tokens": 3},
        "message": OPENING,
        **changes,
    }
    return step(seq, StepKind.SUMMARY, payload)


def one_window(trigger: Any = 10) -> list[Step]:
    fields = {} if trigger is ... else {"summary_trigger": trigger}
    return [
        request_step(1, **fields),
        response_step(2, call("t1"), tokens=(6, 6)),
        result_step(3, "t1"),
        request_step(4, summary_trigger=trigger, summary=True),
        summary_step(5),
    ]


def test_a_summary_replaces_the_conversation_with_its_message() -> None:
    state = replay("go", one_window())

    assert state.messages == [OPENING]
    assert state.needs_model_call
    assert state.pending == []
    assert state.stop_reason is None
    assert state.problem is None


def test_a_summary_starts_the_budget_count_again_and_keeps_the_used_call_ids() -> None:
    state = replay("go", one_window())

    assert (state.counted, state.counted_messages) == (None, 0)
    assert state.used_ids == {"t1"}


def test_the_bound_after_a_summary_is_the_whole_new_request() -> None:
    state = replay("go", one_window())
    tools = ({"name": "read_file", "input_schema": {"type": "object"}},)

    bound = input_bound(
        system="be brief",
        tools=tools,
        messages=state.messages,
        counted=state.counted,
        counted_messages=state.counted_messages,
    )

    assert bound == input_bound(
        system="be brief", tools=tools, messages=[OPENING], counted=None, counted_messages=0
    )
    assert bound > ALLOWANCE + len(CONTINUATION)


def test_the_window_after_a_summary_goes_on_like_any_conversation() -> None:
    state = replay(
        "go",
        [
            *one_window(),
            request_step(6, summary_trigger=10),
            response_step(7, call("t2"), tokens=(4, 1)),
            result_step(8, "t2"),
        ],
    )

    assert state.messages[0] == OPENING
    assert [message["role"] for message in state.messages] == ["user", "assistant", "user"]
    assert state.messages[2]["content"][0]["tool_use_id"] == "t2"
    assert (state.counted, state.counted_messages) == (5, 2)
    assert state.used_ids == {"t1", "t2"}


def test_a_summary_with_a_problem_opens_no_window() -> None:
    log = [*one_window()[:-1], summary_step(5, problem="it calls a tool")]
    del log[-1].payload["message"]

    state = replay("go", log)

    assert state.problem == "it calls a tool"
    assert state.problem_in_summary
    assert state.messages[0] == {"role": "user", "content": "go"}
    assert not state.needs_model_call


@pytest.mark.parametrize(
    "message",
    [
        {"role": "user", "content": [{"type": "text", "text": "other text"}]},
        {"role": "assistant", "content": OPENING["content"]},
        None,
    ],
)
def test_a_summary_whose_message_does_not_match_its_text_opens_no_window(message: Any) -> None:
    """The model is never sent a message that differs from the summary `runtime logs` shows."""
    log = [*one_window()[:-1], summary_step(5, message=message)]

    state = replay("go", log)

    assert state.problem == "its message does not match its text"
    assert state.problem_in_summary
    assert state.messages[0] == {"role": "user", "content": "go"}


def test_a_refused_summary_keeps_its_category() -> None:
    log = [
        *one_window()[:-1],
        summary_step(
            5,
            stop_reason="refusal",
            refusal={"category": "cyber", "explanation": "no"},
            problem="it stopped with stop_reason refusal",
        ),
    ]
    assert replay("go", log).refusal_category == "cyber"


@pytest.mark.parametrize(("logged", "pinned"), [(7, 7), (None, None), (..., None)])
def test_the_first_request_pins_the_trigger_whatever_the_worker_says(
    logged: Any, pinned: int | None
) -> None:
    state = replay("go", one_window(logged)[:1])

    assert state.trigger(500) == pinned
    assert state.trigger(None) == pinned


@pytest.mark.parametrize("logged", [0, -3, True, "10", 1.5, [10]])
def test_a_logged_trigger_that_is_not_a_positive_int_pins_none(logged: Any) -> None:
    assert replay("go", one_window(logged)[:1]).trigger(500) is None


def test_a_later_request_does_not_move_the_pin() -> None:
    state = replay("go", [request_step(1, summary_trigger=7), request_step(2, summary_trigger=99)])
    assert state.trigger(500) == 7


def test_a_job_without_a_request_takes_the_workers_setting() -> None:
    state = replay("go", [])
    assert state.trigger(500) == 500
    assert state.trigger(None) is None


def test_the_summary_is_due_only_when_every_call_is_answered_and_the_count_reached() -> None:
    log = one_window()[:2]
    assert not replay("go", log).summary_due(12)
    log.append(result_step(3, "t1"))
    assert replay("go", log).summary_due(12)
    assert replay("go", log).summary_due(11)
    assert not replay("go", log).summary_due(13)
    assert not replay("go", log).summary_due(None)


def test_a_response_that_ends_the_turn_is_never_summarised() -> None:
    state = replay("go", [request_step(1), response_step(2, *TEXT, tokens=(500, 500))])
    assert not state.summary_due(10)


def test_the_summarys_own_tokens_do_not_fire_the_trigger_again() -> None:
    assert not replay("go", one_window()).summary_due(1)


def test_a_window_that_opens_at_or_over_the_trigger_is_named() -> None:
    log = [*one_window(), request_step(6), response_step(7, *TEXT, tokens=(10, 1))]

    state = replay("go", log)

    assert state.oversize(11) is None
    assert state.oversize(10) == (
        "the summary leaves the next window at or over the trigger: 10 input tokens, trigger 10"
    )
    assert state.oversize(None) is None


def test_only_the_first_response_of_a_window_is_checked_for_size() -> None:
    log = [
        *one_window(),
        request_step(6),
        response_step(7, call("t2"), tokens=(5, 1)),
        result_step(8, "t2"),
        request_step(9),
        response_step(10, *TEXT, tokens=(50, 1)),
    ]
    assert replay("go", log).oversize(10) is None


# The loop, against the fake provider in process and a log in memory


def notes(path: str) -> str:
    """The text of one notes file: long enough that three reads reach the trigger."""
    return "".join(f"{path}, line {n}: a note the later steps need.\n" for n in range(40))


def read_notes(call: ToolCall) -> ToolResult:
    return ToolResult(notes(str(call.input["path"])))


SCHEMA = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
TOOLS = ToolRegistry([Tool("read_file", "Read a file.", SCHEMA, read_notes)])


def scenarios() -> dict[str, Scenario]:
    """The packaged scenarios, with no wait before a summary."""
    with resources.as_file(resources.files("fake_provider") / "scenarios") as packaged:
        found = load_scenarios(packaged)
    for name, scenario in found.items():
        if scenario.summary is not None:
            summary = scenario.summary.model_copy(update={"delay_seconds": 0})
            found[name] = scenario.model_copy(update={"summary": summary})
    return found


class Writer:
    """The step log in memory, folded into the state as the worker's own writer does."""

    def __init__(self, steps: list[Step]) -> None:
        self.steps = list(steps)
        self.tokens_added = 0
        self.state: Any = None

    async def append(self, kind: StepKind, payload: dict[str, Any], *, tokens: int = 0) -> Step:
        written = Step(JOB, len(self.steps) + 1, kind, json.loads(json.dumps(payload)), 1, NOW)
        self.steps.append(written)
        self.tokens_added += tokens
        if self.state is not None:
            self.state.apply(written)
        return written


class Recording:
    """The real client against the fake app, keeping every request it sends."""

    def __init__(
        self, app: Any, stop_after: int | None = None, stop_on_summary: bool = False
    ) -> None:
        self.http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app))
        self.inner = AnthropicProvider(api_key="k", base_url=LOCAL, http_client=self.http)
        self.requests: list[ModelRequest] = []
        # Set after this many answers: the worker hands the job back at the next boundary.
        self.stop_after = stop_after
        # Told to stop when a summary request is made, which then never ends.
        self.stop_on_summary = stop_on_summary
        self.stop = asyncio.Event()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self.stop_on_summary and request.tool_choice == NONE:
            self.stop.set()
            await asyncio.Event().wait()
        answer = await self.inner.complete(request)
        if self.stop_after is not None and len(self.requests) >= self.stop_after:
            self.stop.set()
        return answer

    async def aclose(self) -> None:
        await self.inner.aclose()
        await self.http.aclose()


def job(spec: dict[str, Any], tokens_used: int = 0) -> Job:
    return Job(
        id=JOB,
        status=JobStatus.RUNNING,
        spec=spec,
        token_budget=1_000_000,
        tokens_used=tokens_used,
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


def settings(trigger: int | None) -> agent.AgentSettings:
    return agent.AgentSettings(
        default_model="summary",
        max_tokens=1024,
        workspace_root=Path("/nonexistent"),
        summary_trigger=trigger,
    )


def run_loop(
    app: Any,
    trigger: int | None,
    steps: list[Step] | None = None,
    spec: dict[str, Any] = SPEC,
    stop_after: int | None = None,
    stop_on_summary: bool = False,
) -> tuple[str, Writer, list[ModelRequest], Any]:
    """Run the loop until the job ends or is handed back. Returns (outcome, log, requests, app)."""
    writer = Writer(steps or [])

    async def go() -> str:
        provider = Recording(app, stop_after, stop_on_summary)
        tokens = sum(
            s.payload["usage"]["input_tokens"] + s.payload["usage"]["output_tokens"]
            for s in writer.steps
            if s.kind in (StepKind.MODEL_RESPONSE, StepKind.SUMMARY)
        )
        try:
            status, _ = await asyncio.wait_for(
                agent._loop(
                    writer,  # type: ignore[arg-type]
                    provider,
                    TOOLS,
                    job(spec, tokens),
                    JobSpec.model_validate(spec),
                    settings(trigger),
                    Path("/nonexistent"),
                    Lease(JOB, 1, timedelta(seconds=60), claimed_at=time.monotonic()),
                    list(writer.steps),
                    provider.stop,
                    Halt(JOB),
                ),
                timeout=30,
            )
            return status.value
        except agent._JobFailed as failed:
            return f"failed: {failed}"
        finally:
            requests.extend(provider.requests)
            await provider.aclose()

    requests: list[ModelRequest] = []
    outcome = asyncio.run(go())
    return outcome, writer, requests, app


def kinds(writer: Writer, kind: StepKind) -> list[Step]:
    return [s for s in writer.steps if s.kind is kind]


def logged(app: Any) -> list[dict[str, Any]]:
    """The fake's own request log."""

    async def read() -> list[dict[str, Any]]:
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url=LOCAL
        ) as http:
            found: list[dict[str, Any]] = (await http.get("/_fake/requests")).json()
            return found

    return asyncio.run(read())


def test_a_long_job_summarises_twice_and_succeeds() -> None:
    app = create_app(scenarios())
    outcome, writer, _, _ = run_loop(app, TRIGGER)
    log = logged(app)

    assert outcome == "succeeded"
    assert len(kinds(writer, StepKind.SUMMARY)) == SUMMARIES
    summaries = [entry for entry in log if entry["tool_choice"] != ONE_CALL]
    assert [entry["summary"] for entry in summaries] == [1, 2]
    for entry in summaries:
        index = log.index(entry)
        assert entry["tool_choice"] == {"type": "none"}
        assert entry["body"]["tools"] == log[index - 1]["body"]["tools"]
        assert entry["body"]["messages"][-1]["content"][-1] == {
            "type": "text",
            "text": SUMMARY_INSTRUCTION,
        }
        # The next window opens with the summary as the fake sent it, and the continuation.
        sent = f"{SUMMARY_TEXT}\n\n[summary {entry['summary']}] [turn {entry['turn']}]"
        assert log[index + 1]["body"]["messages"] == [opening_message(sent)]
    assert [entry["turn"] for entry in summaries] == [3, 6]
    # Every turn of the script was answered once, in order, across the windows.
    turns = [entry["turn"] for entry in log if entry["tool_choice"] == ONE_CALL]
    assert turns == list(range(9))


def test_with_no_trigger_the_same_job_never_summarises() -> None:
    app = create_app(scenarios())
    outcome, writer, _, _ = run_loop(app, None)

    assert outcome == "succeeded"
    assert kinds(writer, StepKind.SUMMARY) == []
    assert all(entry["tool_choice"] is None for entry in logged(app))
    assert {s.payload["summary_trigger"] for s in kinds(writer, StepKind.MODEL_REQUEST)} == {None}


def test_every_request_step_names_the_trigger_and_the_tools_offered() -> None:
    app = create_app(scenarios())
    _, writer, requests, _ = run_loop(app, TRIGGER)
    steps = kinds(writer, StepKind.MODEL_REQUEST)

    assert len(steps) == len(requests)
    for written, sent in zip(steps, requests, strict=True):
        assert written.payload["summary_trigger"] == TRIGGER
        assert written.payload["tools"] == [tool["name"] for tool in sent.tools]
        assert written.payload.get("summary", False) is (sent.tool_choice == NONE)


@pytest.mark.parametrize("other", [None, 400, 5000])
@pytest.mark.parametrize("answers", [3, 4, 6])
def test_a_worker_with_another_trigger_sends_what_the_first_would_have(
    other: int | None, answers: int
) -> None:
    """The first worker hands the job back after some answers; another finishes it.

    After 3 the summary is due, after 4 it is stored, after 6 the second
    window is under way.
    """
    reference = run_loop(create_app(scenarios()), TRIGGER)
    outcome, first, _, _ = run_loop(create_app(scenarios()), TRIGGER, stop_after=answers)
    assert outcome == "queued"
    assert 0 < len(first.steps) < len(reference[1].steps)

    outcome, second, _, _ = run_loop(create_app(scenarios()), other, steps=first.steps)

    assert outcome == "succeeded"
    assert [(s.kind, s.payload) for s in second.steps] == [
        (s.kind, s.payload) for s in reference[1].steps
    ]


def test_a_summary_request_left_open_is_sent_again_the_same() -> None:
    outcome, first, sent, _ = run_loop(create_app(scenarios()), TRIGGER, stop_on_summary=True)
    assert outcome == "queued"
    assert first.steps[-1].kind is StepKind.MODEL_REQUEST
    assert first.steps[-1].payload["summary"] is True

    outcome, second, again, _ = run_loop(create_app(scenarios()), None, steps=first.steps)

    assert outcome == "succeeded"
    # A request is a frozen dataclass: equal fields, equal request. The
    # failpoint hook is None on both.
    assert again[0] == sent[-1]
    assert again[0].tool_choice == {"type": "none"}
    assert len(kinds(second, StepKind.SUMMARY)) == SUMMARIES


@pytest.mark.parametrize("first_request", [{"summary_trigger": None}, {}])
def test_a_job_whose_first_request_has_no_trigger_never_summarises(
    first_request: dict[str, Any],
) -> None:
    """The first request step was written without a trigger, by an older worker or none set."""
    app = create_app(scenarios())
    opened = request_step(1, **first_request)

    outcome, writer, _, _ = run_loop(app, TRIGGER, steps=[opened])

    assert outcome == "succeeded"
    assert kinds(writer, StepKind.SUMMARY) == []
    assert all(entry["tool_choice"] is None for entry in logged(app))


@pytest.mark.parametrize(
    ("scenario", "problem"),
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
    scenario: str, problem: str
) -> None:
    app = create_app(scenarios())
    outcome, writer, _, _ = run_loop(app, 100, spec={**SPEC, "model": scenario})

    assert outcome == f"failed: the summary is not usable: {problem}"
    (summary,) = kinds(writer, StepKind.SUMMARY)
    assert summary.payload["problem"] == problem
    assert "message" not in summary.payload
    # The last step: no tool ran after it, and nothing was sent again.
    assert writer.steps[-1] == summary
    assert sum(1 for entry in logged(app) if entry["tool_choice"] != ONE_CALL) == 1
    if scenario == "summary-bad-refusal":
        assert summary.payload["refusal"]["category"] == "cyber"


def test_a_window_that_opens_at_or_over_the_trigger_fails_the_job() -> None:
    """With a trigger of 1 the first window's opening response is over it."""
    outcome, writer, _, _ = run_loop(create_app(scenarios()), 1)
    opening = writer.steps[-1]

    assert opening.kind is StepKind.MODEL_RESPONSE
    assert writer.steps[-3].kind is StepKind.SUMMARY
    size = opening.payload["usage"]["input_tokens"]
    assert outcome == (
        "failed: the summary leaves the next window at or over the trigger:"
        f" {size} input tokens, trigger 1"
    )


def test_the_job_window_opens_under_the_trigger_in_the_long_job() -> None:
    """The same check passes in the long job: each window opens well under the trigger."""
    _, writer, _, _ = run_loop(create_app(scenarios()), TRIGGER)
    after = [writer.steps[s.seq + 1] for s in kinds(writer, StepKind.SUMMARY)]
    assert [step.kind for step in after] == [StepKind.MODEL_RESPONSE] * SUMMARIES
    assert all(step.payload["usage"]["input_tokens"] < TRIGGER for step in after)


# One tool call a response (issue 258)

# The `two-calls` scenario: two reads in its first response, then the end.
TWO_CALLS = {"prompt": "Read a.txt and b.txt, then say you are done.", "model": "two-calls"}
TWO_CALLS_PROBLEM = "it has 2 tool_use blocks where one was asked"
# A trigger the `two-calls` job never reaches: it has a trigger and no summary.
UNREACHED = 100_000


def test_a_job_with_a_trigger_asks_for_one_call_in_every_request_but_a_summary() -> None:
    app = create_app(scenarios())
    outcome, writer, requests, _ = run_loop(app, TRIGGER)
    summaries = [s.payload.get("summary", False) for s in kinds(writer, StepKind.MODEL_REQUEST)]
    expected = [NONE if summary else ONE_CALL for summary in summaries]

    assert outcome == "succeeded"
    assert summaries.count(True) == SUMMARIES
    assert [entry["tool_choice"] for entry in logged(app)] == expected
    assert [request.tool_choice for request in requests] == expected


def test_a_job_without_a_trigger_sends_no_tool_choice() -> None:
    app = create_app(scenarios())
    outcome, _, requests, _ = run_loop(app, None)
    log = logged(app)

    assert outcome == "succeeded"
    assert len(log) == len(requests) == 9
    assert [request.tool_choice for request in requests] == [None] * 9
    assert [entry for entry in log if "tool_choice" in entry["body"]] == []


def test_two_calls_in_a_response_to_a_one_call_request_fail_the_job_before_a_tool_runs() -> None:
    app = create_app(scenarios())
    outcome, writer, _, _ = run_loop(app, UNREACHED, spec=TWO_CALLS)

    assert outcome == f"failed: the model's response is not usable: {TWO_CALLS_PROBLEM}"
    # The response is the last step: no tool ran after it.
    assert [s.kind for s in writer.steps] == [StepKind.MODEL_REQUEST, StepKind.MODEL_RESPONSE]
    response = writer.steps[-1]
    assert response.payload["problem"] == TWO_CALLS_PROBLEM
    assert [b["type"] for b in response.payload["content"]].count("tool_use") == 2
    # One request, which asked for one call, and nothing sent after the answer.
    assert [entry["tool_choice"] for entry in logged(app)] == [ONE_CALL]


def test_without_a_trigger_two_calls_in_a_response_both_run() -> None:
    """The same script with no trigger: nothing asked for one call, so both calls run."""
    app = create_app(scenarios())
    outcome, writer, _, _ = run_loop(app, None, spec=TWO_CALLS)
    log = logged(app)

    assert outcome == "succeeded"
    assert len(kinds(writer, StepKind.TOOL_RESULT)) == 2
    assert len(log) == 2
    results = [b for b in log[1]["body"]["messages"][-1]["content"] if b["type"] == "tool_result"]
    assert len(results) == 2


@pytest.mark.parametrize(("pinned", "own"), [(UNREACHED, None), (None, UNREACHED)])
def test_the_pinned_trigger_decides_whether_two_calls_fail_the_job(
    pinned: int | None, own: int | None
) -> None:
    """The job's first request step pinned `pinned`; the worker that takes it over has `own`."""
    app = create_app(scenarios())
    opened = request_step(1, summary_trigger=pinned)

    outcome, writer, _, _ = run_loop(app, own, steps=[opened], spec=TWO_CALLS)
    responses = kinds(writer, StepKind.MODEL_RESPONSE)

    if pinned is not None:
        assert outcome == f"failed: the model's response is not usable: {TWO_CALLS_PROBLEM}"
        assert writer.steps[-1] == responses[-1]
        assert responses[-1].payload["problem"] == TWO_CALLS_PROBLEM
        assert kinds(writer, StepKind.TOOL_INTENT) == []
        assert [entry["tool_choice"] for entry in logged(app)] == [ONE_CALL]
    else:
        assert outcome == "succeeded"
        assert len(kinds(writer, StepKind.TOOL_RESULT)) == 2
        assert [entry["tool_choice"] for entry in logged(app)] == [None, None]


@pytest.mark.parametrize("other", [None, 400, 5000])
@pytest.mark.parametrize("answers", [3, 4, 6])
def test_a_takeover_sends_the_tool_choice_the_first_worker_would_have(
    other: int | None, answers: int
) -> None:
    """The job's first request pinned the trigger, so the other worker's own setting is not read."""
    _, _, reference, _ = run_loop(create_app(scenarios()), TRIGGER)
    outcome, first, before, _ = run_loop(create_app(scenarios()), TRIGGER, stop_after=answers)
    assert outcome == "queued"

    outcome, _, after, _ = run_loop(create_app(scenarios()), other, steps=first.steps)

    assert outcome == "succeeded"
    expected = [request.tool_choice for request in reference]
    assert expected.count(NONE) == SUMMARIES
    assert expected.count(ONE_CALL) == len(expected) - SUMMARIES
    assert [request.tool_choice for request in before] == expected[: len(before)]
    assert [request.tool_choice for request in after] == expected[len(before) :]
    assert ONE_CALL in [request.tool_choice for request in after]


# A halt during an open summary call


class Hanging:
    """The real client, except that a summary request is never answered."""

    def __init__(self, app: Any) -> None:
        self.recording = Recording(app)
        self.open = asyncio.Event()
        self.given_up = False

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.tool_choice != NONE:
            return await self.recording.complete(request)
        self.open.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.given_up = True
        raise AssertionError("unreachable")

    async def aclose(self) -> None:
        await self.recording.aclose()


@pytest.mark.parametrize("how", ["cancel", "lost lease"])
def test_a_halt_or_a_lost_lease_gives_up_an_open_summary_call(how: str) -> None:
    writer = Writer([])
    halt = Halt(JOB)
    lease = Lease(JOB, 1, timedelta(seconds=60), claimed_at=time.monotonic())

    async def go() -> tuple[BaseException | None, float, bool]:
        provider = Hanging(create_app(scenarios()))
        loop = asyncio.create_task(
            agent._loop(
                writer,  # type: ignore[arg-type]
                provider,
                TOOLS,
                job(SPEC),
                JobSpec.model_validate(SPEC),
                settings(TRIGGER),
                Path("/nonexistent"),
                lease,
                [],
                asyncio.Event(),
                halt,
            )
        )
        await asyncio.wait_for(provider.open.wait(), 10)
        asked = time.monotonic()
        if how == "cancel":
            halt.set(HaltReason.CANCELLED)
        else:
            lease.mark_lost()
        try:
            await asyncio.wait_for(loop, 10)
        except (Halted, LeaseLost) as stopped:
            return stopped, time.monotonic() - asked, provider.given_up
        finally:
            await provider.aclose()
        return None, time.monotonic() - asked, provider.given_up

    stopped, took, given_up = asyncio.run(go())

    expected = Halted if how == "cancel" else LeaseLost
    assert isinstance(stopped, expected)
    assert given_up
    assert took < 5
    assert writer.steps[-1].kind is StepKind.MODEL_REQUEST
    assert writer.steps[-1].payload["summary"] is True
