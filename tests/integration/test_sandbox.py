"""Sandboxed tool calls against the real Docker daemon.

Needs the sandbox image that `make up` builds. The tests run on the host, so
the job workspaces are a directory under the test's temporary directory. A
named volume is bound onto that directory: the sandbox mounts one job's
directory of it by subpath, as it does with the stack's workspace volume.
"""

import asyncio
import dataclasses
import os
import subprocess
import time
import uuid
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from runtime.sandbox.docker import Docker
from runtime.sandbox.runner import LABEL, Run, Sandbox, SandboxSettings
from runtime.store.steps import StepKind
from runtime.tools import default_tools

from .conftest import Ran, Stack

SOCKET = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
IMAGE = os.environ.get("SANDBOX_IMAGE", "agent-runtime-sandbox:dev")
# Short, so the timeout cases do not slow the suite down. Only the tests of
# the timeout use it: 100 MB of output can take several seconds on a slow
# machine.
SHORT_TIMEOUT = 2.0
# Far above what any other command here needs. The 100 MB flood took 1.1 s
# to 4.0 s in 30 runs on one arm64 machine (issue 31).
TIMEOUT = 60.0
LIMIT = 64 * 1024


def docker_cli(*args: str) -> str:
    return subprocess.run(  # noqa: S603 - fixed command, arguments come from the tests
        ["docker", *args],  # noqa: S607 - the docker CLI from PATH, as in the chaos tests
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout


def our_containers(volume: str) -> list[str]:
    """Sandbox containers that mount this test's volume, running or not."""
    return docker_cli(
        "ps", "--all", "--quiet", "--filter", f"label={LABEL}", "--filter", f"volume={volume}"
    ).split()


@pytest.fixture
def workspace_volume(tmp_path: Path) -> Iterator[tuple[Path, str]]:
    """The workspace root on the host, and a volume bound onto it."""
    root = tmp_path / "workspaces"
    root.mkdir()
    name = f"test-workspaces-{uuid.uuid4().hex}"
    docker_cli(
        "volume", "create", "--driver", "local",
        "--opt", "type=none", "--opt", "o=bind", "--opt", f"device={root}",
        name,
    )  # fmt: skip
    try:
        yield root, name
    finally:
        docker_cli("volume", "rm", "--force", name)


@pytest.fixture
def settings(workspace_volume: tuple[Path, str]) -> SandboxSettings:
    # The test's own uid, which owns the workspace directories on the host.
    # The stack's worker and sandbox share uid 10001 the same way.
    return SandboxSettings(
        image=IMAGE,
        volume=workspace_volume[1],
        user=f"{os.getuid()}:{os.getgid()}",
        timeout=TIMEOUT,
    )


@pytest.fixture
def short(settings: SandboxSettings) -> SandboxSettings:
    return dataclasses.replace(settings, timeout=SHORT_TIMEOUT)


def new_workspace(root: Path) -> uuid.UUID:
    job_id = uuid.uuid4()
    (root / str(job_id)).mkdir()
    return job_id


class OwnClientSandbox:
    """A sandbox that opens a Docker client for each call.

    The tests run each job in an event loop of its own, and a client
    belongs to the loop it was used in.
    """

    def __init__(self, settings: SandboxSettings) -> None:
        self.settings = settings

    async def run(self, job_id: uuid.UUID, argv: Sequence[str]) -> Run:
        docker = Docker(SOCKET)
        try:
            return await Sandbox(docker, self.settings).run(job_id, argv)
        finally:
            await docker.aclose()


def run(settings: SandboxSettings, job_id: uuid.UUID, argv: Sequence[str]) -> Run:
    return asyncio.run(OwnClientSandbox(settings).run(job_id, argv))


def shell(settings: SandboxSettings, job_id: uuid.UUID, script: str) -> Run:
    return run(settings, job_id, ["/bin/sh", "-c", script])


def test_output_and_exit_status_come_back(
    settings: SandboxSettings, workspace_volume: tuple[Path, str]
) -> None:
    job_id = new_workspace(workspace_volume[0])

    done = shell(settings, job_id, "echo out; echo err >&2; exit 3")

    assert done.exit_code == 3
    assert sorted(done.output.splitlines()) == [b"err", b"out"]
    assert done.total == 8
    assert not done.truncated
    assert our_containers(workspace_volume[1]) == []


def test_the_container_has_only_its_jobs_directory_and_no_privileges(
    settings: SandboxSettings, workspace_volume: tuple[Path, str]
) -> None:
    root = workspace_volume[0]
    job_id = new_workspace(root)
    other = new_workspace(root)
    (root / str(other) / "secret-of-another-job.txt").write_text("not for this job")

    done = shell(
        settings,
        job_id,
        "id -u; "
        "touch /written-to-root 2>/dev/null && echo root-writable; "
        "grep -E '^(CapEff|NoNewPrivs):' /proc/self/status; "
        "ls /sys/class/net; "
        "find / -path /proc -prune -o -name 'secret-of-*' -print 2>/dev/null; "
        "echo kept > note.txt",
    )

    assert done.exit_code == 0, done.output
    lines = done.output.decode().split()
    assert lines[0] == str(os.getuid())
    assert "root-writable" not in lines
    assert lines[1:5] == ["CapEff:", "0000000000000000", "NoNewPrivs:", "1"]
    # The loopback interface only: no network.
    assert lines[5:] == ["lo"]
    # The other job's file was not found anywhere, and the note is on the host.
    assert (root / str(job_id) / "note.txt").read_text() == "kept\n"


def test_a_command_past_its_timeout_is_killed_and_its_container_removed(
    short: SandboxSettings, workspace_volume: tuple[Path, str]
) -> None:
    job_id = new_workspace(workspace_volume[0])

    started = time.monotonic()
    done = shell(short, job_id, "echo started; sleep 30")
    took = time.monotonic() - started

    assert done.timed_out
    assert done.output == b"started\n"
    assert took < SHORT_TIMEOUT + 5
    assert our_containers(workspace_volume[1]) == []


def test_output_past_the_limit_is_counted_and_not_kept(
    settings: SandboxSettings, workspace_volume: tuple[Path, str]
) -> None:
    job_id = new_workspace(workspace_volume[0])

    done = shell(settings, job_id, "yes | head -c 100000000")

    assert done.exit_code == 0
    assert done.total == 100_000_000
    assert len(done.output) == LIMIT
    assert done.truncated


@pytest.mark.parametrize("cancels", [1, 2])
def test_a_cancelled_call_still_removes_its_container(
    settings: SandboxSettings, workspace_volume: tuple[Path, str], cancels: int
) -> None:
    """Also when the removal itself is cancelled once more, as a shutdown can do."""
    job_id = new_workspace(workspace_volume[0])

    async def go() -> None:
        docker = Docker(SOCKET)
        call = asyncio.ensure_future(Sandbox(docker, settings).run(job_id, ["sleep", "30"]))
        await asyncio.sleep(1.0)
        for _ in range(cancels):
            call.cancel()
            await asyncio.sleep(0.005)
        with pytest.raises(asyncio.CancelledError):
            await call
        # The shielded removal goes on without the cancelled call.
        for _ in range(100):
            if not our_containers(workspace_volume[1]):
                break
            await asyncio.sleep(0.1)
        await docker.aclose()

    asyncio.run(go())
    assert our_containers(workspace_volume[1]) == []


# The G9 cases through a whole job: each becomes a tool result, and the job goes on.


def sent_results(ran: Ran) -> list[dict[str, object]]:
    """The tool results the model was sent, by the fake provider's own log."""
    messages = ran.requests[-1]["body"]["messages"]
    return [block for message in messages[1:] if message["role"] == "user"
            for block in message["content"]]  # fmt: skip


def test_a_timed_out_command_becomes_a_tool_result_and_the_job_goes_on(
    stack: Stack, workspace_volume: tuple[Path, str], short: SandboxSettings
) -> None:
    assert stack.workspace_root == workspace_volume[0]
    tools = default_tools(sandbox=OwnClientSandbox(short))

    ran = stack.run_one({"prompt": "Run one command.", "model": "sandbox-timeout"}, tools)

    assert ran.job.status == "succeeded", ran.job.error
    results = [step.payload for step in ran.steps if step.kind is StepKind.TOOL_RESULT]
    assert len(results) == 1
    timed_out = results[0]
    assert timed_out["is_error"] is True
    assert timed_out["content"] == (
        f"started\n[stopped after {SHORT_TIMEOUT:g} s: the command did not finish]"
    )
    assert "truncated" not in timed_out
    assert our_containers(workspace_volume[1]) == []

    assert [entry["turn"] for entry in ran.requests] == [0, 1]
    sent = sent_results(ran)
    assert [block["is_error"] for block in sent] == [True]
    assert [block["content"] for block in sent] == [timed_out["content"]]


def test_bad_output_becomes_tool_results_and_the_job_goes_on(
    stack: Stack, workspace_volume: tuple[Path, str], settings: SandboxSettings
) -> None:
    assert stack.workspace_root == workspace_volume[0]
    tools = default_tools(sandbox=OwnClientSandbox(settings))

    ran = stack.run_one({"prompt": "Run three commands.", "model": "sandbox-bad-output"}, tools)

    assert ran.job.status == "succeeded", ran.job.error
    results = [step.payload for step in ran.steps if step.kind is StepKind.TOOL_RESULT]
    assert len(results) == 3
    failed, flood, undecodable = results

    assert failed["is_error"] is True
    assert failed["content"] == "failing\n[exit status 7]"
    assert flood["is_error"] is False
    assert flood["truncated"] is True
    assert len(flood["content"].encode()) <= LIMIT
    assert flood["content"].endswith(
        f"[output cut to fit {LIMIT} bytes; the command wrote 100000000]\n[exit status 0]"
    )
    assert undecodable["is_error"] is False
    assert undecodable["content"] == "bad \ufffd\ufffd bytes \ufffd end\n[exit status 0]"
    assert not any("truncated" in result for result in (failed, undecodable))
    assert our_containers(workspace_volume[1]) == []

    # Every result, in the last request of the job.
    assert [entry["turn"] for entry in ran.requests] == [0, 1, 2, 3]
    sent = sent_results(ran)
    assert [block["is_error"] for block in sent] == [True, False, False]
    assert [block["content"] for block in sent] == [result["content"] for result in results]
