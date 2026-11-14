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
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from runtime.provider.base import ModelRequest, ModelResponse
from runtime.provider.messages import AnthropicProvider
from runtime.sandbox.docker import Docker
from runtime.sandbox.runner import LABEL, Run, Sandbox, SandboxSettings, ends_within
from runtime.store.jobs import claim_job
from runtime.store.status import JobStatus
from runtime.store.steps import Step, StepKind, read_steps
from runtime.tools import default_tools
from runtime.worker.agent import run_job

from .conftest import Stack, expire_lease

SOCKET = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
IMAGE = os.environ.get("SANDBOX_IMAGE", "agent-runtime-sandbox:dev")
# Short, so the timeout case does not slow the suite down.
TIMEOUT = 2.0
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
        # A test that failed can leave a container that holds the volume.
        for container in our_containers(name):
            docker_cli("rm", "--force", container)
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


def new_workspace(root: Path) -> uuid.UUID:
    job_id = uuid.uuid4()
    (root / str(job_id)).mkdir()
    return job_id


class OwnClientSandbox:
    """A sandbox that opens a Docker client for each call.

    The tests run each job in an event loop of its own, and a client
    belongs to the loop it was used in.
    """

    # What the sandbox is: a real one, or one of a worker that dies.
    kind: type[Sandbox] = Sandbox

    def __init__(self, settings: SandboxSettings) -> None:
        self.settings = settings

    async def run(self, job_id: uuid.UUID, lease_epoch: int, argv: Sequence[str]) -> Run:
        docker = Docker(SOCKET)
        try:
            return await self.kind(docker, self.settings).run(job_id, lease_epoch, argv)
        finally:
            await docker.aclose()

    async def remove_older(self, job_id: uuid.UUID, lease_epoch: int) -> int:
        docker = Docker(SOCKET)
        try:
            return await self.kind(docker, self.settings).remove_older(job_id, lease_epoch)
        finally:
            await docker.aclose()


def run(settings: SandboxSettings, job_id: uuid.UUID, argv: Sequence[str]) -> Run:
    return asyncio.run(OwnClientSandbox(settings).run(job_id, 1, argv))


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
    settings: SandboxSettings, workspace_volume: tuple[Path, str]
) -> None:
    job_id = new_workspace(workspace_volume[0])

    started = time.monotonic()
    done = shell(settings, job_id, "echo started; sleep 30")
    took = time.monotonic() - started

    assert done.timed_out
    assert done.output == b"started\n"
    assert took < TIMEOUT + 5
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
        call = asyncio.ensure_future(Sandbox(docker, settings).run(job_id, 1, ["sleep", "30"]))
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


def test_bad_output_becomes_tool_results_and_the_job_goes_on(
    stack: Stack, workspace_volume: tuple[Path, str], settings: SandboxSettings
) -> None:
    assert stack.workspace_root == workspace_volume[0]
    tools = default_tools(sandbox=OwnClientSandbox(settings))

    ran = stack.run_one({"prompt": "Run four commands.", "model": "sandbox-bad-output"}, tools)

    assert ran.job.status == "succeeded", ran.job.error
    results = [step.payload for step in ran.steps if step.kind is StepKind.TOOL_RESULT]
    assert len(results) == 4
    timed_out, failed, flood, undecodable = results

    assert timed_out["is_error"] is True
    assert timed_out["content"] == (
        f"started\n[stopped after {TIMEOUT:g} s: the command did not finish]"
    )
    assert failed["is_error"] is True
    assert failed["content"] == "failing\n[exit status 7]"
    assert flood["is_error"] is False
    assert flood["truncated"] is True
    assert len(flood["content"].encode()) <= LIMIT
    assert flood["content"].endswith(
        f"[output cut to fit {LIMIT} bytes; the command wrote 100000000]\n[exit status 0]"
    )
    assert undecodable["is_error"] is False
    assert undecodable["content"] == "bad �� bytes � end\n[exit status 0]"
    assert not any("truncated" in result for result in (timed_out, failed, undecodable))
    assert our_containers(workspace_volume[1]) == []

    # What the model was sent, by the fake provider's own log: every result,
    # in the last request of the job.
    assert [entry["turn"] for entry in ran.requests] == [0, 1, 2, 3, 4]
    messages = ran.requests[-1]["body"]["messages"]
    sent = [block for message in messages[1:] if message["role"] == "user"
            for block in message["content"]]  # fmt: skip
    assert [block["is_error"] for block in sent] == [True, True, False, False]
    assert [block["content"] for block in sent] == [result["content"] for result in results]


# A worker that stops during a sandboxed call (ADR 0008): its container goes
# on. The worker that takes the job over removes it, and decides nothing
# about the call before the call's bound has passed.


class NeverRemoves(Sandbox):
    """The sandbox of a worker that is gone: nothing removes its container."""

    async def _remove(self, name: str) -> None:
        return None


class DyingWorkersSandbox(OwnClientSandbox):
    kind = NeverRemoves


class OneLongCommand:
    """A provider that asks for one command, then ends the turn."""

    def __init__(self, command: str) -> None:
        self.command = command

    async def complete(self, request: ModelRequest) -> ModelResponse:
        turn = sum(1 for message in request.messages if message["role"] == "assistant")
        if turn == 0:
            content = [
                {
                    "type": "tool_use",
                    "id": "toolu_long",
                    "name": "run_shell",
                    "input": {"command": self.command},
                }
            ]
            return ModelResponse("msg_0", "m", content, "tool_use", 10, 5)
        return ModelResponse("msg_1", "m", [{"type": "text", "text": "done"}], "end_turn", 10, 5)

    async def aclose(self) -> None:
        return None


def running(container: str) -> bool:
    return docker_cli("inspect", "--format", "{{.State.Running}}", container).strip() == "true"


@dataclass
class TakenOver:
    status: JobStatus | None
    steps: list[Step]
    # Seconds after the second worker started until the first worker's
    # container was gone, or None if it was still there when the worker ended.
    removed_after: float | None
    # Seconds the second worker took.
    took: float


def kill_during_a_call_then_take_over(
    stack: Stack, workspace_volume: tuple[Path, str], settings: SandboxSettings
) -> TakenOver:
    provider = OneLongCommand("echo started; sleep 300")
    volume = workspace_volume[1]

    async def body(engine: AsyncEngine, _: AnthropicProvider) -> TakenOver:
        async with engine.begin() as conn:
            await stack.create(conn, {"prompt": "run it"})
            first_claim = await claim_job(conn, worker_id="w1")
        assert first_claim is not None
        first = asyncio.create_task(
            run_job(
                engine,
                provider,
                default_tools(sandbox=DyingWorkersSandbox(settings)),
                first_claim,
                stack.settings(),
            )
        )
        async with asyncio.timeout(30):
            while not (found := await asyncio.to_thread(our_containers, volume)):  # noqa: ASYNC110 - Docker has no event to wait on
                await asyncio.sleep(0.1)
            while not await asyncio.to_thread(running, found[0]):  # noqa: ASYNC110 - Docker has no event to wait on
                await asyncio.sleep(0.1)
        # The worker is gone. Its container is not.
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        assert await asyncio.to_thread(running, found[0])

        async with engine.begin() as conn:
            await expire_lease(conn, first_claim.id)
            second_claim = await claim_job(conn, worker_id="w2")
        assert second_claim is not None
        assert second_claim.lease_epoch == first_claim.lease_epoch + 1
        started = time.monotonic()
        second = asyncio.create_task(
            run_job(
                engine,
                provider,
                default_tools(sandbox=OwnClientSandbox(settings)),
                second_claim,
                stack.settings(),
            )
        )
        removed_after = None
        while not second.done():
            if removed_after is None and not await asyncio.to_thread(our_containers, volume):
                removed_after = time.monotonic() - started
            await asyncio.sleep(0.2)
        status = await second
        took = time.monotonic() - started
        if removed_after is None and not our_containers(volume):
            removed_after = took
        async with engine.connect() as conn:
            steps = await read_steps(conn, first_claim.id)
        return TakenOver(status, steps, removed_after, took)

    return stack.run(body)


def test_the_next_worker_removes_a_dead_workers_container_and_waits_out_the_call(
    stack: Stack, workspace_volume: tuple[Path, str], settings: SandboxSettings
) -> None:
    assert stack.workspace_root == workspace_volume[0]
    # A short bound, so the test waits seconds and not a minute.
    short = dataclasses.replace(settings, timeout=5.0, start_within=2.0)
    bound = ends_within(short)

    taken = kill_during_a_call_then_take_over(stack, workspace_volume, short)

    # Removed at the takeover, long before the command's own timeout ends it.
    assert taken.removed_after is not None
    assert taken.removed_after < 5.0
    assert our_containers(workspace_volume[1]) == []

    kinds = [(step.kind, step.lease_epoch) for step in taken.steps]
    assert kinds == [
        (StepKind.MODEL_REQUEST, 1),
        (StepKind.MODEL_RESPONSE, 1),
        (StepKind.TOOL_INTENT, 1),
        (StepKind.APPROVAL_REQUESTED, 2),
    ]
    intent, question = taken.steps[2], taken.steps[3]
    assert intent.payload["ends_within"] == bound
    # run_shell is not idempotent: after the wait, a person decides.
    assert taken.status is JobStatus.NEEDS_APPROVAL
    # Not before the bound had passed since the intent, by the database clock.
    assert (question.created_at - intent.created_at).total_seconds() >= bound


def test_a_container_left_by_a_dead_worker_ends_at_its_own_timeout(
    settings: SandboxSettings, workspace_volume: tuple[Path, str]
) -> None:
    """Nobody removes it, and it still does not run on: the timeout is inside the container."""
    job_id = new_workspace(workspace_volume[0])
    short = dataclasses.replace(settings, timeout=2.0)

    async def abandon() -> str:
        call = asyncio.ensure_future(
            DyingWorkersSandbox(short).run(job_id, 1, ["/bin/sh", "-c", "sleep 300 & sleep 300"])
        )
        async with asyncio.timeout(30):
            while not (found := await asyncio.to_thread(our_containers, workspace_volume[1])):  # noqa: ASYNC110 - Docker has no event to wait on
                await asyncio.sleep(0.1)
        call.cancel()
        await asyncio.gather(call, return_exceptions=True)
        return found[0]

    container = asyncio.run(abandon())
    try:
        started = time.monotonic()
        while running(container) and time.monotonic() - started < 30:
            time.sleep(0.2)

        assert not running(container)
        # The worker's timeout plus the margin, counted from the start.
        assert time.monotonic() - started < short.timeout + 5
    finally:
        docker_cli("rm", "--force", container)
