"""The workspace watcher of ADR 0014: the measurement, and a call over the limit.

The integration test in `test_sandbox.py` runs it against Docker.
"""

import asyncio
import os
import uuid
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import pytest

from runtime.sandbox.runner import MIB, Run, SandboxSettings
from runtime.sandbox.workspace import Unmeasurable, workspace_size
from runtime.scope import Resolved
from runtime.tools.base import ToolCall, ToolResult
from runtime.tools.run_shell import run_shell_tool

JOB = uuid.UUID("01a1d655-88bd-7468-afc4-5ca3a1947906")


def blocks(path: Path) -> int:
    return os.lstat(path).st_blocks * 512


# Measuring


def test_files_and_directories_are_counted_by_what_they_take_on_disk(tmp_path: Path) -> None:
    (tmp_path / "a").write_bytes(b"x" * 100_000)
    (tmp_path / "d" / "e").mkdir(parents=True)
    (tmp_path / "d" / "e" / "b").write_bytes(b"y" * 5000)
    paths = [tmp_path, tmp_path / "a", tmp_path / "d", tmp_path / "d" / "e", tmp_path / "d/e/b"]

    assert workspace_size(tmp_path) == sum(blocks(p) for p in paths)
    assert workspace_size(tmp_path) >= 105_000


def test_a_sparse_file_counts_what_it_uses(tmp_path: Path) -> None:
    with (tmp_path / "sparse").open("wb") as file:
        file.truncate(512 * MIB)
    assert workspace_size(tmp_path) < MIB


def test_a_link_is_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "big").write_bytes(b"z" * MIB)
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "to-dir").symlink_to(outside)
    (root / "to-file").symlink_to(outside / "big")

    assert workspace_size(root) < MIB


@pytest.fixture
def locked(tmp_path: Path) -> Iterator[Callable[[int], Path]]:
    """A directory below the workspace with the mode the test gives it, restored afterwards."""
    inner = tmp_path / "inner"
    inner.mkdir()
    (inner / "f").write_bytes(b"q")

    def lock(mode: int) -> Path:
        inner.chmod(mode)
        return tmp_path

    yield lock
    inner.chmod(0o700)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any directory")
@pytest.mark.parametrize("mode", [0o000, 0o300, 0o600])
def test_a_directory_the_command_locked_cannot_be_measured(
    locked: Callable[[int], Path], mode: int
) -> None:
    """The command runs as the worker's uid and may chmod its own directories."""
    with pytest.raises(Unmeasurable):
        workspace_size(locked(mode))


def test_a_missing_workspace_cannot_be_measured(tmp_path: Path) -> None:
    with pytest.raises(Unmeasurable):
        workspace_size(tmp_path / "gone")


def test_directories_nested_too_deep_cannot_be_measured(tmp_path: Path) -> None:
    path = tmp_path
    for _ in range(1200):
        path = path / "d"
        path.mkdir()
    with pytest.raises(Unmeasurable, match="nested too deep"):
        workspace_size(tmp_path)


# The watcher around a call


class WritingSandbox:
    """Writes `chunk` bytes into the workspace every `every` seconds, `times` times, then ends."""

    def __init__(self, workspace: Path, limit: int | None, *, times: int, chunk: int) -> None:
        self.settings = SandboxSettings(
            image="sandbox:test",
            volume="workspaces",
            workspace_limit=limit,
            watch_interval=0.05,
        )
        self.workspace = workspace
        self.times = times
        self.chunk = chunk
        self.calls = 0
        self.cancelled = False
        self.written = 0

    async def run(
        self,
        job_id: uuid.UUID,
        lease_epoch: int,
        argv: Sequence[str],
        scope: Resolved | None = None,
    ) -> Run:
        self.calls += 1
        try:
            for n in range(self.times):
                (self.workspace / f"part-{n}").write_bytes(b"w" * self.chunk)
                self.written += self.chunk
                await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return Run(b"wrote it all\n", 13, 0)

    async def remove_older(self, job_id: uuid.UUID, lease_epoch: int) -> int:
        raise AssertionError("not called")


def run(sandbox: WritingSandbox) -> ToolResult:
    tool = run_shell_tool(sandbox)
    call = ToolCall(sandbox.workspace, {"command": "write"}, f"{JOB}:toolu_1", 1)
    return asyncio.run(tool.run(call))


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / str(JOB)
    root.mkdir()
    return root


def test_a_call_that_writes_past_the_limit_is_stopped(workspace: Path) -> None:
    sandbox = WritingSandbox(workspace, 2 * MIB, times=100, chunk=MIB // 4)

    result = run(sandbox)

    assert result.is_error
    assert result.content.startswith("[stopped: the job's workspace holds ")
    assert "over its limit of 2.0 MiB" in result.content
    assert sandbox.cancelled
    # Stopped near the limit: far less than the 25 MiB it would write.
    assert sandbox.written < 8 * MIB


def test_a_call_within_the_limit_runs_to_its_end(workspace: Path) -> None:
    sandbox = WritingSandbox(workspace, 4 * MIB, times=4, chunk=MIB // 4)

    result = run(sandbox)

    assert result == ToolResult("wrote it all\n[exit status 0]")
    assert not sandbox.cancelled


def test_a_workspace_over_the_limit_starts_no_command(workspace: Path) -> None:
    (workspace / "big").write_bytes(b"b" * (2 * MIB))
    sandbox = WritingSandbox(workspace, MIB, times=1, chunk=1)

    result = run(sandbox)

    assert result.is_error
    assert result.content.endswith("over its limit of 1.0 MiB. The command did not start.")
    assert sandbox.calls == 0


def test_a_workspace_that_cannot_be_measured_starts_no_command(tmp_path: Path) -> None:
    sandbox = WritingSandbox(tmp_path / str(JOB), MIB, times=1, chunk=1)

    result = run(sandbox)

    assert result.is_error
    assert "cannot be measured" in result.content
    assert sandbox.calls == 0


def test_without_a_limit_nothing_is_measured(tmp_path: Path) -> None:
    # The workspace does not even exist: a measurement would refuse the call.
    sandbox = WritingSandbox(tmp_path / str(JOB), None, times=0, chunk=0)

    assert run(sandbox) == ToolResult("wrote it all\n[exit status 0]")
