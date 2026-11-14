"""Sandboxed tools: the command built from the model's input, and the result it gets back."""

import asyncio
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from runtime.sandbox.docker import SandboxError
from runtime.sandbox.runner import Run, SandboxSettings
from runtime.store.payload import check_json
from runtime.tools.base import ToolCall, ToolResult
from runtime.tools.list_dir import list_dir_tool
from runtime.tools.run_shell import run_shell_tool
from runtime.tools.sandboxed import SandboxTool, describe

LIMIT = 64 * 1024
JOB = uuid.UUID("01a1d655-88bd-7468-afc4-5ca3a1947906")


DONE = Run(b"done\n", 5, 0)


class FakeSandbox:
    """Records the commands it is asked to run and answers with a fixed run."""

    def __init__(self, answer: Run | Exception = DONE) -> None:
        self.settings = SandboxSettings(image="sandbox:test", volume="workspaces", timeout=30)
        self.answer = answer
        self.calls: list[tuple[uuid.UUID, list[str]]] = []

    async def run(self, job_id: uuid.UUID, argv: Sequence[str]) -> Run:
        self.calls.append((job_id, list(argv)))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def call(tool: SandboxTool, tool_input: dict[str, Any]) -> ToolResult:
    workspace = Path("/workspaces") / str(JOB)
    return asyncio.run(tool.run(ToolCall(workspace, tool_input, f"{JOB}:toolu_1")))


# The command built from the input


def test_run_shell_runs_the_command_with_sh_in_the_jobs_workspace() -> None:
    sandbox = FakeSandbox()
    result = call(run_shell_tool(sandbox), {"command": "ls | wc -l"})

    assert sandbox.calls == [(JOB, ["/bin/sh", "-c", "ls | wc -l"])]
    assert result == ToolResult("done\n[exit status 0]")


@pytest.mark.parametrize(
    "tool_input", [{}, {"command": ""}, {"command": "   "}, {"command": 3}, {"command": "a\x00b"}]
)
def test_run_shell_refuses_input_without_a_command_and_starts_nothing(
    tool_input: dict[str, Any],
) -> None:
    sandbox = FakeSandbox()
    result = call(run_shell_tool(sandbox), tool_input)

    assert result.is_error
    assert sandbox.calls == []


@pytest.mark.parametrize(
    ("tool_input", "argv"),
    [
        ({}, ["ls", "-la", "--", "."]),
        ({"path": "."}, ["ls", "-la", "--", "."]),
        ({"path": "notes/old"}, ["ls", "-la", "--", "notes/old"]),
        # After `--` a name that starts with a dash is a name, not an option.
        ({"path": "-R"}, ["ls", "-la", "--", "-R"]),
    ],
)
def test_list_dir_lists_one_path_of_the_workspace(
    tool_input: dict[str, Any], argv: list[str]
) -> None:
    sandbox = FakeSandbox()
    call(list_dir_tool(sandbox), tool_input)

    assert sandbox.calls == [(JOB, argv)]


@pytest.mark.parametrize("path", ["/etc", "../other-job", "a/../../b", "", 7, "a\x00"])
def test_list_dir_refuses_a_path_that_is_not_plain_and_relative(path: object) -> None:
    sandbox = FakeSandbox()
    result = call(list_dir_tool(sandbox), {"path": path})

    assert result.is_error
    assert sandbox.calls == []


def test_only_list_dir_is_idempotent() -> None:
    assert list_dir_tool(FakeSandbox()).idempotent
    assert not run_shell_tool(FakeSandbox()).idempotent


def test_a_sandbox_that_fails_gives_an_error_result() -> None:
    sandbox = FakeSandbox(SandboxError("Docker could not be reached"))
    result = call(run_shell_tool(sandbox), {"command": "true"})

    assert result.is_error
    assert "Docker could not be reached" in result.content


# The result the model gets


def test_a_non_zero_exit_is_an_error_result_with_the_status() -> None:
    result = describe(Run(b"no such file\n", 13, 2), timeout=30, limit=LIMIT)

    assert result == ToolResult("no such file\n[exit status 2]", is_error=True)


def test_a_timeout_is_an_error_result_that_says_so() -> None:
    result = describe(Run(b"started\n", 8, None), timeout=30, limit=LIMIT)

    assert result.is_error
    assert result.content == "started\n[stopped after 30 s: the command did not finish]"


def test_cut_output_is_flagged_and_says_how_much_there_was() -> None:
    result = describe(Run(b"a" * LIMIT, 100_000_000, 0), timeout=30, limit=LIMIT)

    assert result.truncated
    assert not result.is_error
    assert result.content.endswith(
        "[output cut to fit 65536 bytes; the command wrote 100000000]\n[exit status 0]"
    )
    assert len(result.content.encode()) <= LIMIT


def test_invalid_utf8_and_nul_bytes_are_replaced_so_the_result_can_be_stored() -> None:
    result = describe(Run(b"ok \xff\xfe \x00 end", 11, 0), timeout=30, limit=LIMIT)

    assert result.content == "ok �� � end\n[exit status 0]"
    check_json(result.content)


@given(
    output=st.binary(max_size=3000),
    extra=st.integers(min_value=0, max_value=10**9),
    exit_code=st.one_of(st.none(), st.integers(min_value=0, max_value=255)),
    limit=st.integers(min_value=200, max_value=2000),
)
def test_the_result_never_passes_the_limit_and_can_always_be_stored(
    output: bytes, extra: int, exit_code: int | None, limit: int
) -> None:
    kept = output[:limit]
    result = describe(Run(kept, len(output) + extra, exit_code), timeout=30, limit=limit)

    assert len(result.content.encode()) <= limit
    check_json(result.content)
    # Output that does not reach the model whole is flagged.
    if not result.truncated:
        assert len(output) + extra == len(kept)
        assert result.content.startswith(kept.decode("utf-8", "replace").replace("\x00", "�"))
