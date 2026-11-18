"""Sandboxed tool calls against the real Docker daemon.

Needs the sandbox image that `make up` builds. The tests run on the host, so
the job workspaces are a directory under the test's temporary directory. A
named volume is bound onto that directory: the sandbox mounts one job's
directory of it by subpath, as it does with the stack's workspace volume.
"""

import asyncio
import dataclasses
import ipaddress
import os
import re
import subprocess
import time
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from runtime.provider.base import ModelRequest, ModelResponse
from runtime.provider.messages import AnthropicProvider
from runtime.sandbox.docker import Docker
from runtime.sandbox.runner import (
    DEFAULT_WORKSPACE_LIMIT_MIB,
    LABEL,
    MIB,
    Run,
    Sandbox,
    SandboxSettings,
    ends_within,
)
from runtime.sandbox.workspace import Bounds, workspace_size
from runtime.scope import FIXED_DENY, Resolved
from runtime.store.jobs import DEADLINE_ERROR, Job, cancel_job, claim_job, create_job, get_job
from runtime.store.status import JobStatus
from runtime.store.steps import Step, StepKind, read_steps
from runtime.tools import default_tools
from runtime.tools.base import ToolCall, ToolResult
from runtime.tools.run_shell import run_shell_tool
from runtime.tools.sandboxed import SandboxTool
from runtime.worker.agent import run_job

from .conftest import Ran, Stack, database_now, expire_lease
from .test_resume import PARKED

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
        # A test that failed can leave a container that holds the volume.
        for container in our_containers(name):
            docker_cli("rm", "--force", container)
        docker_cli("volume", "rm", "--force", name)


@pytest.fixture
def settings(workspace_volume: tuple[Path, str]) -> SandboxSettings:
    # The test's own uid, which owns the workspace directories on the host.
    # The stack's worker and sandbox share uid 10001 the same way.
    # The workspace limit is the worker's default: its config always sets one,
    # so every call here goes through the watcher as in the stack.
    return SandboxSettings(
        image=IMAGE,
        volume=workspace_volume[1],
        user=f"{os.getuid()}:{os.getgid()}",
        timeout=TIMEOUT,
        workspace_limit=DEFAULT_WORKSPACE_LIMIT_MIB * MIB,
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

    # What the sandbox is: a real one, or one of a worker that dies.
    kind: type[Sandbox] = Sandbox

    def __init__(self, settings: SandboxSettings) -> None:
        self.settings = settings

    async def run(
        self,
        job_id: uuid.UUID,
        lease_epoch: int,
        argv: Sequence[str],
        scope: Resolved | None = None,
    ) -> Run:
        docker = Docker(SOCKET)
        try:
            return await self.kind(docker, self.settings).run(job_id, lease_epoch, argv, scope)
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


def test_a_long_listing_with_a_bad_name_becomes_a_cut_result_and_the_job_goes_on(
    stack: Stack, workspace_volume: tuple[Path, str], settings: SandboxSettings
) -> None:
    """list_dir runs through the same runner as run_shell. Its bad output is cut the same way."""
    assert stack.workspace_root == workspace_volume[0]
    tools = default_tools(sandbox=OwnClientSandbox(settings))

    spec = {"prompt": "List a big directory.", "model": "sandbox-list-dir-bad-output"}
    ran = stack.run_one(spec, tools)

    assert ran.job.status == "succeeded", ran.job.error
    calls = [step.payload for step in ran.steps if step.kind is StepKind.TOOL_INTENT]
    assert [call["name"] for call in calls] == ["run_shell", "list_dir"]
    results = [step.payload for step in ran.steps if step.kind is StepKind.TOOL_RESULT]
    made, listing = results
    assert made["content"] == "made\n[exit status 0]"

    assert listing["is_error"] is False
    assert listing["truncated"] is True
    assert len(listing["content"].encode()) <= LIMIT
    assert re.search(
        rf"\[output cut to fit {LIMIT} bytes; the command wrote \d+\]\n\[exit status 0\]$",
        listing["content"],
    )
    # The bad name sorts first, before the cut. Its two bytes became U+FFFD.
    assert " a��name\n" in listing["content"]
    assert "file-with-a-long-name-to-fill-the-listing-1\n" in listing["content"]
    assert our_containers(workspace_volume[1]) == []

    # The model was sent the cut listing as it was stored.
    assert [entry["turn"] for entry in ran.requests] == [0, 1, 2]
    assert [block["content"] for block in sent_results(ran)] == [
        made["content"],
        listing["content"],
    ]


# A worker that stops during a sandboxed call (ADR 0008): its container goes
# on. The worker that takes the job over removes it, and decides nothing
# about the call before the call's bound has passed.


class NeverRemoves(Sandbox):
    """The sandbox of a worker that is gone: nothing removes its container."""

    async def _remove(self, name: str) -> bool:
        return True


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
    # A short bound, so the test waits seconds and not a minute. The command's
    # own timeout (10 s plus the margin) ends it far later than the removal.
    short = dataclasses.replace(settings, timeout=10.0, start_within=2.0)
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
        (StepKind.STATUS_CHANGED, 2),
    ]
    assert taken.steps[4].payload == PARKED
    intent, question = taken.steps[2], taken.steps[3]
    assert intent.payload["ends_within"] == bound
    # run_shell is not idempotent: after the wait, a person decides.
    assert taken.status is JobStatus.NEEDS_APPROVAL
    # Not before the bound had passed since the intent, by the database clock.
    assert (question.created_at - intent.created_at).total_seconds() >= bound


@pytest.mark.parametrize(
    "script",
    [
        "sleep 300 & sleep 300",
        # The command tries to stop or kill the timeout that bounds it.
        "kill -STOP $PPID; kill -STOP 1; kill -KILL 1; sleep 300",
    ],
)
def test_a_container_left_by_a_dead_worker_ends_at_its_own_timeout(
    settings: SandboxSettings, workspace_volume: tuple[Path, str], script: str
) -> None:
    """Nobody removes it, and it still does not run on: the timeout is inside the container."""
    job_id = new_workspace(workspace_volume[0])
    short = dataclasses.replace(settings, timeout=2.0)

    async def abandon() -> str:
        call = asyncio.ensure_future(
            DyingWorkersSandbox(short).run(job_id, 1, ["/bin/sh", "-c", script])
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


# PTRACE_ATTACH (16) through perl's syscall(), which needs the number of
# ptrace on this machine's architecture. Perl tries PID 1, then a child it
# forked itself: Yama at 1 lets a process trace only its own descendants.
PTRACE = {"x86_64": 101, "aarch64": 117}
ATTACH = (
    "perl -e 'sub attach {{ my $r = syscall({nr}, 16, $_[0] + 0, 0, 0);"
    ' print $r == 0 ? "attached\\n" : "refused: $!\\n" }}'
    " attach(1); my $child = fork(); if ($child == 0) {{ sleep 30; exit 0 }}"
    " attach($child); kill 9, $child'"
)


def test_the_command_cannot_trace_its_timeout_but_can_trace_its_own_child(
    settings: SandboxSettings, workspace_volume: tuple[Path, str]
) -> None:
    """An attach stops the traced process, so a traced PID 1 would never end the call.

    The worker refuses a host whose ptrace_scope allows it (`check_host`). The
    control: tracing a child of the command works, so a refusal is not just
    a sandbox without ptrace.
    """
    job_id = new_workspace(workspace_volume[0])
    script = ATTACH.format(nr=PTRACE[os.uname().machine])

    done = shell(settings, job_id, script)

    assert done.output.decode().splitlines() == ["refused: Operation not permitted", "attached"]


# A job with a scope (ADR 0009): the worker resolves it before the intent,
# writes the addresses into the log, and stores the guard's count.

GUARD_IMAGE = os.environ.get("GUARD_IMAGE", "agent-runtime-guard:dev")


@pytest.fixture
def guard_network() -> Iterator[tuple[str, str]]:
    """A network for guards, with one listening target on it: (network, target address)."""
    network = f"test-guards-{uuid.uuid4().hex}"
    target = f"test-target-{uuid.uuid4().hex[:8]}"
    docker_cli("network", "create", network)
    try:
        docker_cli(
            "run", "--detach", "--name", target, "--network", network, IMAGE, "perl",
            "-MIO::Socket::INET", "-e",
            '$| = 1; $s = IO::Socket::INET->new(LocalPort => 8080, Listen => 5, ReuseAddr => 1)'
            ' or die; print "listening\\n"; while ($c = $s->accept) { print $c "hi\\n"; close $c }',
        )  # fmt: skip
        deadline = time.monotonic() + 30
        while "listening" not in docker_cli("logs", target):
            assert time.monotonic() < deadline
            time.sleep(0.1)
        address = docker_cli(
            "inspect", "-f", f'{{{{(index .NetworkSettings.Networks "{network}").IPAddress}}}}',
            target,
        ).strip()  # fmt: skip
        yield network, address
    finally:
        for container in docker_cli("ps", "-aq", "--filter", f"network={network}").split():
            docker_cli("rm", "--force", container)
        docker_cli("network", "rm", network)


def scoped_job(
    stack: Stack, settings: SandboxSettings, scope: list[dict[str, Any]], command: str
) -> TakenOver:
    provider = OneLongCommand(command)

    async def body(engine: AsyncEngine, _: AnthropicProvider) -> TakenOver:
        async with engine.begin() as conn:
            await stack.create(conn, {"prompt": "reach it", "scope": scope})
            claimed = await claim_job(conn, worker_id="w1")
        assert claimed is not None
        status = await run_job(
            engine,
            provider,
            default_tools(sandbox=OwnClientSandbox(settings)),
            claimed,
            stack.settings(),
        )
        async with engine.connect() as conn:
            steps = await read_steps(conn, claimed.id)
        return TakenOver(status, steps, None, 0.0)

    return stack.run(body)


def test_a_scoped_call_records_its_addresses_and_the_dropped_packets(
    stack: Stack,
    workspace_volume: tuple[Path, str],
    settings: SandboxSettings,
    guard_network: tuple[str, str],
) -> None:
    assert stack.workspace_root == workspace_volume[0]
    network, target = guard_network
    scoped = dataclasses.replace(settings, guard_image=GUARD_IMAGE, network=network)

    ran = scoped_job(
        stack,
        scoped,
        [{"host": target, "ports": [8080]}],
        f"timeout 3 bash -c 'exec 3<>/dev/tcp/{target}/8080 && head -1 <&3';"
        " timeout 3 bash -c 'exec 3<>/dev/tcp/1.1.1.1/443' || echo blocked",
    )

    assert ran.status is JobStatus.SUCCEEDED
    intent = next(step for step in ran.steps if step.kind is StepKind.TOOL_INTENT)
    result = next(step for step in ran.steps if step.kind is StepKind.TOOL_RESULT)
    assert intent.payload["scope"] == [
        {"host": target, "addresses": [f"{target}/32"], "ports": [8080]}
    ]
    assert result.payload["content"].startswith("hi\nblocked\n[exit status 0]")
    assert result.payload["dropped_packets"] > 0


def test_a_scope_that_touches_the_workers_deny_list_starts_no_call(
    stack: Stack,
    workspace_volume: tuple[Path, str],
    settings: SandboxSettings,
    guard_network: tuple[str, str],
) -> None:
    assert stack.workspace_root == workspace_volume[0]
    network, target = guard_network
    # As in the stack: the guards' own network is in the deny list.
    denied = ipaddress.ip_network(f"{target}/16", strict=False)
    scoped = dataclasses.replace(
        settings, guard_image=GUARD_IMAGE, network=network, deny=(*FIXED_DENY, denied)
    )

    ran = scoped_job(stack, scoped, [{"host": target}], "echo should not run")

    kinds = [step.kind for step in ran.steps]
    assert StepKind.TOOL_INTENT not in kinds
    result = next(step for step in ran.steps if step.kind is StepKind.TOOL_RESULT)
    assert result.payload["is_error"] is True
    assert "is in the deny list" in result.payload["content"]
    assert our_containers(workspace_volume[1]) == []


# G7: a cancel or the deadline in the middle of a sandboxed call kills the
# command and removes its container within 5 s (ADR 0010).


def halted_during_a_call(
    stack: Stack,
    workspace_volume: tuple[Path, str],
    settings: SandboxSettings,
    *,
    cancel: bool,
    deadline_in: timedelta,
) -> tuple[JobStatus | None, Job, list[Step], float, list[str]]:
    """Also returns the containers left when the job's run returned.

    Looked up before the event loop closes: closing it cancels any task left
    behind, and that would remove a container the call itself left.
    """
    provider = OneLongCommand("sleep 300")
    volume = workspace_volume[1]

    async def body(engine: AsyncEngine, _: AnthropicProvider) -> tuple[Any, ...]:
        async with engine.begin() as conn:
            await create_job(
                conn,
                spec={"prompt": "wait"},
                token_budget=100_000,
                deadline=datetime.now(UTC) + deadline_in,
            )
            claimed = await claim_job(conn, worker_id="w1")
        assert claimed is not None
        run = asyncio.create_task(
            run_job(
                engine,
                provider,
                default_tools(sandbox=OwnClientSandbox(settings)),
                claimed,
                stack.settings(),
            )
        )
        async with asyncio.timeout(30):
            while not (found := await asyncio.to_thread(our_containers, volume)):  # noqa: ASYNC110 - Docker has no event to wait on
                await asyncio.sleep(0.1)
            while not await asyncio.to_thread(running, found[0]):  # noqa: ASYNC110 - Docker has no event to wait on
                await asyncio.sleep(0.1)
        if cancel:
            started = time.monotonic()
            async with engine.begin() as conn:
                await cancel_job(conn, claimed.id)
        status = await asyncio.wait_for(run, 60)
        if cancel:
            took = time.monotonic() - started
        else:
            # By the clock the worker checks the deadline against.
            async with engine.connect() as conn:
                took = (await database_now(conn) - claimed.deadline).total_seconds()
        left = await asyncio.to_thread(our_containers, volume)
        async with engine.connect() as conn:
            job = await get_job(conn, claimed.id)
            steps = await read_steps(conn, claimed.id)
        assert job is not None
        return status, job, steps, took, left

    status, job, steps, took, left = stack.run(body)
    return status, job, steps, took, left


def test_a_cancel_kills_a_running_command_and_its_container(
    stack: Stack, workspace_volume: tuple[Path, str], settings: SandboxSettings
) -> None:
    assert stack.workspace_root == workspace_volume[0]

    status, job, steps, took, left = halted_during_a_call(
        stack, workspace_volume, settings, cancel=True, deadline_in=timedelta(hours=1)
    )

    assert status is JobStatus.CANCELLED
    assert job.status is JobStatus.CANCELLED
    assert took < 5.0
    assert left == []
    # Killed, not finished: the intent has no result, and the job is final.
    kinds = [step.kind for step in steps]
    assert StepKind.TOOL_INTENT in kinds
    assert StepKind.TOOL_RESULT not in kinds
    # The worker's stop is the last step, with its cause.
    assert (steps[-1].kind, steps[-1].payload) == (
        StepKind.STATUS_CHANGED,
        {"from": "running", "to": "cancelled", "cause": "cancel_requested"},
    )


def test_the_deadline_kills_a_running_command_and_fails_the_job(
    stack: Stack, workspace_volume: tuple[Path, str], settings: SandboxSettings
) -> None:
    assert stack.workspace_root == workspace_volume[0]

    status, job, steps, late, left = halted_during_a_call(
        stack, workspace_volume, settings, cancel=False, deadline_in=timedelta(seconds=4)
    )

    assert status is JobStatus.FAILED
    assert (job.status, job.error) == (JobStatus.FAILED, DEADLINE_ERROR)
    assert 0 <= late < 5.0
    assert left == []
    assert steps[-1].kind is StepKind.ERROR


def test_a_lost_lease_kills_a_running_command(
    stack: Stack, workspace_volume: tuple[Path, str], settings: SandboxSettings
) -> None:
    """Its result would be refused by the fenced write: there is nothing to wait for."""
    assert stack.workspace_root == workspace_volume[0]
    provider = OneLongCommand("sleep 300")
    volume = workspace_volume[1]

    async def body(
        engine: AsyncEngine, _: AnthropicProvider
    ) -> tuple[JobStatus | None, float, list[str]]:
        async with engine.begin() as conn:
            await stack.create(conn, {"prompt": "wait"})
            claimed = await claim_job(conn, worker_id="w1", lease=timedelta(seconds=2))
        assert claimed is not None
        run = asyncio.create_task(
            run_job(
                engine,
                provider,
                default_tools(sandbox=OwnClientSandbox(settings)),
                claimed,
                stack.settings(lease=2.0, heartbeat=0.5),
            )
        )
        async with asyncio.timeout(30):
            while not (found := await asyncio.to_thread(our_containers, volume)):  # noqa: ASYNC110 - Docker has no event to wait on
                await asyncio.sleep(0.1)
            while not await asyncio.to_thread(running, found[0]):  # noqa: ASYNC110 - Docker has no event to wait on
                await asyncio.sleep(0.1)
        # Another worker takes the job: the first one's next renewal is refused.
        async with engine.begin() as conn:
            await expire_lease(conn, claimed.id)
            assert await claim_job(conn, worker_id="w2") is not None
        taken = time.monotonic()
        status = await asyncio.wait_for(run, 30)
        took = time.monotonic() - taken
        # Before the loop closes, as in halted_during_a_call.
        return status, took, await asyncio.to_thread(our_containers, volume)

    status, took, left = stack.run(body)

    # Dropped, not ended: the job is the other worker's now.
    assert status is None
    assert took < 5.0
    assert left == []


# The workspace watcher (ADR 0014)

# Writes 1 MiB every 0.2 s, 60 times: 60 MiB in about 12 s if nothing stops it.
# At 5 MiB/s, the 8 MiB margin of the test below covers 1.6 s of writing past
# the limit: the 0.2 s interval, the walk and the kill, also on a loaded host.
WRITER = (
    "i=0; while [ $i -lt 60 ]; do head -c 1048576 /dev/zero > part-$i;"
    " i=$((i+1)); sleep 0.2; done; echo wrote it all"
)


def measured(path: Path) -> int:
    return workspace_size(path, Bounds(2**40, 10**9, time.monotonic() + 60))


def tool_call(tool: SandboxTool, root: Path, job_id: uuid.UUID, command: str) -> ToolResult:
    call = ToolCall(root / str(job_id), {"command": command}, f"{job_id}:toolu_1", 1)
    return asyncio.run(tool.run(call))


def test_a_command_that_writes_past_the_workspace_limit_is_stopped(
    settings: SandboxSettings, workspace_volume: tuple[Path, str]
) -> None:
    """Soft: the overshoot is what the command writes in one interval plus the kill."""
    root, volume = workspace_volume
    job_id = new_workspace(root)
    limit = dataclasses.replace(settings, workspace_limit=8 * MIB, watch_interval=0.2)
    tool = run_shell_tool(OwnClientSandbox(limit))

    started = time.monotonic()
    result = tool_call(tool, root, job_id, WRITER)
    took = time.monotonic() - started
    size = measured(root / str(job_id))

    assert result.is_error
    assert result.content.startswith("[stopped: the job's workspace holds ")
    assert "holds more than its limit of 8.0 MiB" in result.content
    # Stopped long before the command's 12 s, and close to the limit.
    assert took < 5
    assert 8 * MIB < size < 16 * MIB
    # The container is gone: nothing writes on.
    assert our_containers(volume) == []
    time.sleep(0.5)
    assert measured(root / str(job_id)) == size


def test_a_workspace_over_its_limit_runs_no_command(
    settings: SandboxSettings, workspace_volume: tuple[Path, str]
) -> None:
    root = workspace_volume[0]
    job_id = new_workspace(root)
    (root / str(job_id) / "big").write_bytes(b"b" * (2 * MIB))
    limit = dataclasses.replace(settings, workspace_limit=MIB)

    result = tool_call(run_shell_tool(OwnClientSandbox(limit)), root, job_id, "touch ran")

    assert result.is_error
    assert result.content.endswith("its limit of 1.0 MiB. The command did not start.")
    assert not (root / str(job_id) / "ran").exists()
