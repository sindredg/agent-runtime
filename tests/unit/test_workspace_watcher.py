"""The workspace watcher of ADR 0014 and 0017: the measurement, and a call over the limit.

The integration tests in `test_sandbox.py` run it against Docker.
"""

import asyncio
import dataclasses
import errno
import os
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import pytest

from runtime.sandbox import runner
from runtime.sandbox.runner import MIB, Run, SandboxSettings
from runtime.sandbox.workspace import MAX_DEPTH, Bounds, OverLimit, Unmeasurable, workspace_size
from runtime.scope import Resolved
from runtime.tools import sandboxed
from runtime.tools.base import ToolCall, ToolResult
from runtime.tools.run_shell import run_shell_tool

JOB = uuid.UUID("01a1d655-88bd-7468-afc4-5ca3a1947906")


def blocks(path: Path) -> int:
    return os.lstat(path).st_blocks * 512


def size(root: Path, *, limit: int = 2**40, entries: int = 10**9, seconds: float = 60) -> int:
    return workspace_size(root, Bounds(limit, entries, time.monotonic() + seconds))


# Measuring


def test_files_and_directories_are_counted_by_what_they_take_on_disk(tmp_path: Path) -> None:
    (tmp_path / "a").write_bytes(b"x" * 100_000)
    (tmp_path / "d" / "e").mkdir(parents=True)
    (tmp_path / "d" / "e" / "b").write_bytes(b"y" * 5000)
    paths = [tmp_path, tmp_path / "a", tmp_path / "d", tmp_path / "d" / "e", tmp_path / "d/e/b"]

    assert size(tmp_path) == sum(blocks(p) for p in paths)
    assert size(tmp_path) >= 105_000


def test_a_sparse_file_counts_what_it_uses(tmp_path: Path) -> None:
    with (tmp_path / "sparse").open("wb") as file:
        file.truncate(512 * MIB)
    assert size(tmp_path) < MIB


def test_a_link_is_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "big").write_bytes(b"z" * MIB)
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "to-dir").symlink_to(outside)
    (root / "to-file").symlink_to(outside / "big")

    assert size(root) < MIB


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
        size(locked(mode))


def test_a_missing_workspace_cannot_be_measured(tmp_path: Path) -> None:
    with pytest.raises(Unmeasurable):
        size(tmp_path / "gone")


def test_directories_nested_too_deep_cannot_be_measured(tmp_path: Path) -> None:
    path = tmp_path
    for _ in range(MAX_DEPTH + 1):
        path = path / "d"
        path.mkdir()
    with pytest.raises(Unmeasurable, match=f"nested deeper than {MAX_DEPTH}"):
        size(tmp_path)


def test_the_walk_stops_at_the_size_limit(tmp_path: Path) -> None:
    for n in range(4):
        (tmp_path / f"f{n}").write_bytes(b"x" * MIB)
    with pytest.raises(OverLimit, match=r"its limit of 2\.0 MiB"):
        size(tmp_path, limit=2 * MIB)


def test_the_walk_stops_at_the_entry_limit(tmp_path: Path) -> None:
    for n in range(50):
        (tmp_path / f"f{n}").touch()
    with pytest.raises(OverLimit, match="its limit of 20 files and directories"):
        size(tmp_path, entries=20)


def test_a_walk_past_its_deadline_cannot_be_measured(tmp_path: Path) -> None:
    for n in range(3000):
        (tmp_path / f"f{n}").touch()
    with pytest.raises(Unmeasurable, match="took too long"):
        size(tmp_path, seconds=-1)


def test_a_walk_told_to_stop_cannot_be_measured(tmp_path: Path) -> None:
    for n in range(3000):
        (tmp_path / f"f{n}").touch()
    bounds = Bounds(2**40, 10**9, time.monotonic() + 60)
    bounds.stop.set()
    with pytest.raises(Unmeasurable, match="was stopped"):
        workspace_size(tmp_path, bounds)


@pytest.mark.parametrize("code", [errno.ENOENT, errno.ENOTDIR, errno.ELOOP])
def test_a_directory_replaced_since_the_listing_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    """Deleted, or swapped for a file or a link: what a busy command does, not an error."""
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "f").write_bytes(b"x" * 100_000)
    real_open = os.open

    def swapped(path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if dir_fd is not None and path == "d":
            raise OSError(code, os.strerror(code))
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swapped)
    assert size(tmp_path) == blocks(tmp_path) + blocks(tmp_path / "d")


def test_any_error_of_the_file_system_cannot_be_measured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "d").mkdir()

    def broken(fd: int) -> None:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(os, "scandir", broken)
    with pytest.raises(Unmeasurable, match="Input/output error"):
        size(tmp_path)


# The watcher around a call


class WritingSandbox:
    """Writes `chunk` bytes into the workspace every 0.02 s, `times` times, then ends."""

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
    assert "holds more than its limit of 2.0 MiB" in result.content
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
    assert result.content.endswith("its limit of 1.0 MiB. The command did not start.")
    assert sandbox.calls == 0


def test_a_workspace_with_too_many_files_starts_no_command(workspace: Path) -> None:
    for n in range(30):
        (workspace / f"f{n}").touch()
    sandbox = WritingSandbox(workspace, 64 * MIB, times=1, chunk=1)
    sandbox.settings = SandboxSettings(
        image="sandbox:test", volume="workspaces", workspace_limit=64 * MIB, workspace_entries=20
    )

    result = run(sandbox)

    assert result.is_error
    assert "its limit of 20 files and directories. The command did not start." in result.content
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


def test_a_walk_past_its_own_deadline_starts_no_command(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for n in range(3000):
        (workspace / f"f{n}").touch()
    real_size = workspace_size

    def late(root: Path, bounds: Bounds) -> int:
        # The walk's own deadline passed: only it, not the event loop's clock.
        return real_size(root, dataclasses.replace(bounds, deadline=-1.0))

    monkeypatch.setattr(sandboxed, "workspace_size", late)
    sandbox = WritingSandbox(workspace, 64 * MIB, times=1, chunk=1)

    result = run(sandbox)

    assert result.is_error
    assert "cannot be measured (the walk took too long" in result.content
    assert sandbox.calls == 0


def test_a_walk_that_hangs_past_its_time_box_starts_no_command(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The time box is part of `ends_within`: a walk the file system holds up must not delay
    the start, and is told to stop."""
    stopped: list[bool] = []

    def hangs(root: Path, bounds: Bounds) -> int:
        # As a walk blocked in a system call, or waiting for a thread.
        stopped.append(bounds.stop.wait(5))
        return 0

    monkeypatch.setattr(sandboxed, "workspace_size", hangs)
    monkeypatch.setattr(runner, "MEASURE_WITHIN", 0.3)
    sandbox = WritingSandbox(workspace, 64 * MIB, times=1, chunk=1)

    started = time.monotonic()
    result = run(sandbox)
    took = time.monotonic() - started

    assert result.is_error
    assert "cannot be measured (the walk took more than 0.3 s)" in result.content
    assert sandbox.calls == 0
    assert took < 1.0
    assert stopped == [True]


def test_a_run_that_ends_does_not_wait_for_a_measurement(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    measured = 0
    stopped: list[bool] = []

    def slow(root: Path, bounds: Bounds) -> int:
        nonlocal measured
        measured += 1
        if measured > 1:
            # Every measurement during the call takes 2 s, unless told to stop.
            stopped.append(bounds.stop.wait(2))
        return 0

    monkeypatch.setattr(sandboxed, "workspace_size", slow)
    sandbox = WritingSandbox(workspace, 64 * MIB, times=10, chunk=1)
    tool = run_shell_tool(sandbox)

    async def body() -> tuple[ToolResult, float]:
        started = time.monotonic()
        result = await tool.run(ToolCall(workspace, {"command": "w"}, f"{JOB}:toolu_1", 1))
        # Timed in the loop: closing it waits for the walk's thread.
        return result, time.monotonic() - started

    started = time.monotonic()
    result, took = asyncio.run(body())
    closed = time.monotonic() - started

    assert result == ToolResult("wrote it all\n[exit status 0]")
    assert measured >= 2
    assert took < 1.5
    # The walk under way when the run ended was told to stop: it did not run on.
    assert stopped[-1] is True
    assert closed < 1.5


class SlowRemoval(WritingSandbox):
    """Writes past the limit at once, then takes `removal` seconds to go when cancelled."""

    def __init__(self, workspace: Path, removal: float) -> None:
        super().__init__(workspace, MIB, times=1, chunk=2 * MIB)
        self.removal = removal
        self.removing = asyncio.Event()

    async def run(
        self,
        job_id: uuid.UUID,
        lease_epoch: int,
        argv: Sequence[str],
        scope: Resolved | None = None,
    ) -> Run:
        (self.workspace / "big").write_bytes(b"w" * self.chunk)
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            self.removing.set()
            # As Sandbox.run's finally: the removal is waited for.
            await asyncio.sleep(self.removal)
            raise
        raise AssertionError("not stopped")


def test_a_cancel_of_the_call_during_the_removal_is_not_swallowed(workspace: Path) -> None:
    """The job's cancel or deadline cancels the tool run; the watcher must pass that on."""
    sandbox = SlowRemoval(workspace, removal=0.5)
    tool = run_shell_tool(sandbox)

    async def body() -> None:
        call = asyncio.ensure_future(
            tool.run(ToolCall(workspace, {"command": "w"}, f"{JOB}:toolu_1", 1))
        )
        await asyncio.wait_for(sandbox.removing.wait(), 5)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call

    asyncio.run(body())
