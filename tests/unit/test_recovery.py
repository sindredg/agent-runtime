"""What to do with a tool call that was interrupted: one test per row of the decision table."""

from pathlib import Path
from typing import Any

import pytest

from runtime.tools.base import CheckOutcome, Tool, ToolCall, ToolResult, operation_id
from runtime.worker.recovery import Ask, Recovered, Rerun, decide

CALL = ToolCall(workspace=Path("unused"), input={"title": "x"}, operation_id="job:toolu_1")


def never_run(call: ToolCall) -> ToolResult:
    raise AssertionError("deciding must not run the tool")


def tool(*, idempotent: bool = False, check: Any = None) -> Tool:
    return Tool("t", "A tool.", {"type": "object"}, never_run, idempotent=idempotent, check=check)


def test_an_idempotent_tool_is_run_again() -> None:
    assert isinstance(decide(tool(idempotent=True), CALL, approved=False), Rerun)


def test_an_idempotent_tool_is_run_again_without_asking_its_check() -> None:
    def check(call: ToolCall) -> ToolResult:
        raise AssertionError("an idempotent tool needs no check")

    assert isinstance(decide(tool(idempotent=True, check=check), CALL, approved=False), Rerun)


def test_a_check_that_answers_done_gives_the_result_and_the_tool_does_not_run() -> None:
    seen: list[ToolCall] = []

    def check(call: ToolCall) -> ToolResult:
        seen.append(call)
        return ToolResult("issue 7 exists")

    decision = decide(tool(check=check), CALL, approved=False)
    assert decision == Recovered(ToolResult("issue 7 exists"))
    # The check got the same call the tool would have got, operation id included.
    assert seen == [CALL]


def test_a_check_that_answers_not_done_means_run_again() -> None:
    decision = decide(tool(check=lambda call: CheckOutcome.NOT_DONE), CALL, approved=False)
    assert isinstance(decision, Rerun)


def test_a_check_that_answers_unknown_means_ask_a_person() -> None:
    decision = decide(tool(check=lambda call: CheckOutcome.UNKNOWN), CALL, approved=False)
    assert isinstance(decision, Ask)
    assert "could not tell" in decision.reason


def raises(call: ToolCall) -> ToolResult:
    raise ConnectionError("tracker unreachable")


def exits(call: ToolCall) -> ToolResult:
    raise SystemExit(2)


@pytest.mark.parametrize(
    "check",
    [
        raises,
        exits,
        lambda call: None,
        lambda call: "done",
        lambda call: ToolResult(b"bytes"),  # type: ignore[arg-type]
        lambda call: ToolResult("a\x00b"),
    ],
    ids=["raises", "exits", "none", "a-string", "bytes-content", "unstorable-content"],
)
def test_a_check_that_fails_or_answers_nonsense_means_ask_a_person(check: Any) -> None:
    decision = decide(tool(check=check), CALL, approved=False)
    assert isinstance(decision, Ask)
    assert "check" in decision.reason


def test_a_tool_with_neither_flag_nor_check_means_ask_a_person() -> None:
    decision = decide(tool(), CALL, approved=False)
    assert isinstance(decision, Ask)
    assert "not idempotent" in decision.reason


def test_an_approved_call_is_run_again_without_asking_the_check() -> None:
    def check(call: ToolCall) -> ToolResult:
        raise AssertionError("approval already answered the question")

    assert isinstance(decide(tool(check=check), CALL, approved=True), Rerun)
    assert isinstance(decide(tool(), CALL, approved=True), Rerun)


def test_a_tool_the_worker_does_not_know_is_run_again() -> None:
    """Running it only produces the 'unknown tool' error result. Nothing can happen twice."""
    assert isinstance(decide(None, CALL, approved=False), Rerun)


def test_the_operation_id_is_the_job_and_the_tool_use_id() -> None:
    assert operation_id("0190-job", "toolu_1") == "0190-job:toolu_1"


def test_a_tool_is_not_idempotent_and_has_no_check_unless_it_says_so() -> None:
    plain = Tool("t", "A tool.", {"type": "object"}, never_run)
    assert plain.idempotent is False
    assert plain.check is None
