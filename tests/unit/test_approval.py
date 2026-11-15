"""Which tool calls wait for a person, and what a job's spec may say about it (ADR 0011)."""

from typing import Any

import pytest
from pydantic import ValidationError

from runtime.spec import MAX_GATED_TOOLS, JobSpec
from runtime.tools.base import Tool, ToolCall, ToolRegistry, ToolResult
from runtime.worker.approval import gate_reason, not_offered


def never_run(call: ToolCall) -> ToolResult:
    raise AssertionError("the gate must not run the tool")


def tool(name: str = "act", *, needs_approval: bool = False) -> Tool:
    return Tool(name, "A tool.", {"type": "object"}, never_run, needs_approval=needs_approval)


def test_a_tool_needs_no_approval_unless_its_definition_says_so() -> None:
    assert tool().needs_approval is False
    assert gate_reason(tool(), "act", ()) is None


def test_a_tool_whose_definition_says_so_is_gated_whatever_the_spec_says() -> None:
    flagged = tool(needs_approval=True)
    for named in ((), ("other",), ("act",)):
        reason = gate_reason(flagged, "act", named)
        assert reason is not None
        assert "the tool" in reason


def test_a_spec_adds_a_tool_to_the_gate() -> None:
    reason = gate_reason(tool(), "act", ("act",))
    assert reason is not None
    assert "spec" in reason
    assert gate_reason(tool("other"), "other", ("act",)) is None


def test_the_definition_sent_to_the_model_does_not_carry_the_flag() -> None:
    assert tool(needs_approval=True).definition() == tool().definition()


def test_names_in_a_spec_that_the_worker_does_not_offer_are_found() -> None:
    tools = ToolRegistry([tool("act"), tool("read_file")])
    assert not_offered(("act",), tools) == []
    assert not_offered(("act", "run_shel", "act", "x"), tools) == ["run_shel", "x"]


def test_a_spec_names_the_tools_that_need_approval() -> None:
    assert JobSpec.model_validate({"prompt": "go"}).needs_approval is None
    spec = JobSpec.model_validate({"prompt": "go", "needs_approval": ["run_shell", "act"]})
    assert spec.needs_approval == ("run_shell", "act")
    # An empty list adds nothing, and takes nothing away.
    assert JobSpec.model_validate({"prompt": "go", "needs_approval": []}).needs_approval == ()


@pytest.mark.parametrize(
    "value",
    [
        # There is no way to write "this tool needs none".
        {"run_shell": False},
        [{"name": "run_shell", "needs_approval": False}],
        False,
        "run_shell",
        [""],
        [5],
        ["x" * 201],
        ["t"] * (MAX_GATED_TOOLS + 1),
    ],
)
def test_a_spec_cannot_loosen_the_gate_or_name_what_is_not_a_tool(value: Any) -> None:
    with pytest.raises(ValidationError):
        JobSpec.model_validate({"prompt": "go", "needs_approval": value})
