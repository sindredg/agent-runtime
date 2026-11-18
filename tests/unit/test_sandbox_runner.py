"""The runner against a stand-in for Docker: what a takeover removes, when a call is given up."""

import asyncio
import dataclasses
import ipaddress
import threading
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from runtime.sandbox import runner
from runtime.sandbox.docker import REQUEST_TIMEOUT, DockerError, SandboxError
from runtime.sandbox.runner import (
    EPOCH_LABEL,
    JOB_LABEL,
    LABEL,
    MIB,
    NotStarted,
    Run,
    Sandbox,
    SandboxSettings,
    call_within,
    check_host,
    deny_list,
    ends_within,
    measure_within,
)
from runtime.scope import FIXED_DENY, Resolved
from runtime.tools.base import ToolCall, ToolResult
from runtime.tools.run_shell import run_shell_tool

JOB = uuid.UUID("01a1d655-88bd-7468-afc4-5ca3a1947906")


class FakeDocker:
    """Answers the calls the runner makes and records them."""

    def __init__(
        self,
        found: list[dict[str, Any]] | None = None,
        create_takes: float = 0,
        start_takes: float = 0,
        refuse_removal: frozenset[str] = frozenset(),
        late: frozenset[str] = frozenset(),
    ) -> None:
        self.found = found or []
        self.create_takes = create_takes
        self.start_takes = start_takes
        self.refuse_removal = refuse_removal
        # Requests answered only after an hour.
        self.late = late
        self.calls: list[tuple[str, Any]] = []
        # A guard's token, by container name: its stream says it is ready.
        self.ready: dict[str, str] = {}

    async def _answer(self, request: str) -> None:
        if request in self.late:
            await asyncio.sleep(3600)

    async def containers(self, labels: dict[str, str]) -> list[dict[str, Any]]:
        self.calls.append(("containers", labels))
        return self.found

    async def create(self, name: str, config: dict[str, Any]) -> None:
        self.calls.append(("create", name))
        for entry in config.get("Env", []):
            if entry.startswith("READY="):
                self.ready[name] = entry.removeprefix("READY=")
        await self._answer("create")
        await asyncio.sleep(self.create_takes)

    @asynccontextmanager
    async def attach(self, name: str) -> AsyncIterator[AsyncIterator[bytes]]:
        self.calls.append(("attach", name))
        await self._answer("attach")
        token = self.ready.get(name)

        async def stream() -> AsyncIterator[bytes]:
            # A guard says it is ready, in one stdout frame. A command says nothing.
            if token is not None:
                text = f"ready {token}\n".encode()
                yield bytes([1, 0, 0, 0]) + len(text).to_bytes(4, "big") + text

        yield stream()

    async def kill(self, name: str, signal: str) -> None:
        self.calls.append(("kill", name))

    async def start(self, name: str) -> None:
        self.calls.append(("start", name))
        await self._answer("start")
        await asyncio.sleep(self.start_takes)

    async def wait(self, name: str) -> int:
        self.calls.append(("wait", name))
        await self._answer("wait")
        return 0

    async def remove(self, name: str) -> None:
        self.calls.append(("remove", name))
        await self._answer("remove")
        if name in self.refuse_removal:
            raise DockerError(409, "removal of container is already in progress")


def settings(**changes: Any) -> SandboxSettings:
    return SandboxSettings(image="sandbox:test", volume="workspaces", **changes)


def container(id_: str, epoch: str | None) -> dict[str, Any]:
    labels = {JOB_LABEL: str(JOB)} | ({EPOCH_LABEL: epoch} if epoch is not None else {})
    return {"Id": id_, "Labels": labels}


def test_takeover_removes_the_jobs_containers_of_earlier_epochs_only() -> None:
    docker = FakeDocker(
        [
            container("older", "3"),
            container("much-older", "1"),
            container("own", "4"),
            container("newer", "5"),
            container("no-epoch", None),
            container("bad-epoch", "x"),
        ]
    )
    sandbox = Sandbox(docker, settings())  # type: ignore[arg-type]

    removed = asyncio.run(sandbox.remove_older(JOB, 4))

    assert removed == 4
    # Only containers the sandbox started.
    assert docker.calls[0] == ("containers", {LABEL: "1", JOB_LABEL: str(JOB)})
    assert [name for call, name in docker.calls if call == "remove"] == [
        "older",
        "much-older",
        "no-epoch",
        "bad-epoch",
    ]


def test_docker_is_not_asked_to_start_once_the_start_bound_has_passed() -> None:
    """A slow set-up must not start a command the takeover bound no longer covers."""
    docker = FakeDocker(create_takes=0.3)
    sandbox = Sandbox(docker, settings(start_within=0.1))  # type: ignore[arg-type]

    with pytest.raises(NotStarted, match=r"took more than 0\.1 s"):
        asyncio.run(sandbox.run(JOB, 1, ["true"]))

    calls = [call for call, _ in docker.calls]
    assert "start" not in calls
    # Created, so removed again.
    assert calls[-1] == "remove"


def test_a_quick_set_up_starts_the_command() -> None:
    docker = FakeDocker()
    sandbox = Sandbox(docker, settings(start_within=5))  # type: ignore[arg-type]

    run = asyncio.run(sandbox.run(JOB, 1, ["true"]))

    assert run.exit_code == 0
    assert [call for call, _ in docker.calls] == ["create", "attach", "start", "wait", "remove"]


def test_one_container_that_cannot_be_removed_does_not_stop_the_others() -> None:
    docker = FakeDocker(
        [container("first", "1"), container("second", "1")], refuse_removal=frozenset({"first"})
    )
    sandbox = Sandbox(docker, settings())  # type: ignore[arg-type]

    with pytest.raises(SandboxError, match=r"1 container\(s\) not removed: first"):
        asyncio.run(sandbox.remove_older(JOB, 2))

    assert [name for call, name in docker.calls if call == "remove"] == ["first", "second"]


def test_a_start_request_is_bounded_as_a_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not per phase: the bound of a takeover counts on it."""
    monkeypatch.setattr(runner, "REQUEST_TIMEOUT", 0.1)
    docker = FakeDocker(start_takes=5)
    sandbox = Sandbox(docker, settings())  # type: ignore[arg-type]

    with pytest.raises(SandboxError, match="did not answer the start") as raised:
        asyncio.run(sandbox.run(JOB, 1, ["true"]))

    # Asked to start, so it may have run.
    assert not isinstance(raised.value, NotStarted)
    assert [call for call, _ in docker.calls][-1] == "remove"


EVERY_REQUEST = frozenset({"create", "attach", "start", "wait", "remove"})


@pytest.fixture
def short(monkeypatch: pytest.MonkeyPatch) -> SandboxSettings:
    """Settings whose whole-call bound is under a second: 0.2 + 0.1 + 0.2 + 0.1 + 0.1."""
    monkeypatch.setattr(runner, "REQUEST_TIMEOUT", 0.1)
    monkeypatch.setattr(runner, "KILL_MARGIN", 0.1)
    return settings(start_within=0.2, timeout=0.2)


def timed[T](call: Callable[[], Awaitable[T]]) -> tuple[float, T | BaseException]:
    """Seconds `call` took, and what it returned or raised. One still going after 5 s fails.

    The call runs on an event loop of its own, in a thread the test can give
    up on. A call without a bound would wait for the daemon's hour, and
    cancelling it would not help: its removal is shielded.
    """
    outcome: list[T | BaseException] = []

    def run() -> None:
        async def body() -> T:
            return await call()

        try:
            outcome.append(asyncio.run(body()))
        except Exception as error:
            outcome.append(error)

    began = time.monotonic()
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout=5)
    took = time.monotonic() - began
    if not outcome:
        pytest.fail(f"the call was still going after {took:.1f} s")
    return took, outcome[0]


def timed_run(sandbox: Sandbox, scope: Resolved | None = None) -> tuple[float, Run | BaseException]:
    return timed(lambda: sandbox.run(JOB, 1, ["true"], scope))


def test_a_whole_call_is_bounded_by_the_containers_bound_plus_one_request() -> None:
    assert call_within(settings()) == ends_within(settings()) + REQUEST_TIMEOUT
    # The defaults: 15 + 10 + 30 + 2 + 10.
    assert call_within(settings()) == 67


def test_a_daemon_that_answers_every_request_late_ends_the_call_within_its_bound(
    short: SandboxSettings,
) -> None:
    sandbox = Sandbox(FakeDocker(late=EVERY_REQUEST), short)  # type: ignore[arg-type]

    took, outcome = timed_run(sandbox)

    assert took < call_within(short) + 0.3
    # The create never came back, so Docker was never asked to start it.
    assert isinstance(outcome, NotStarted), outcome
    assert f"within {call_within(short):g} s" in str(outcome)


def test_with_a_workspace_limit_the_run_leaves_the_measurement_its_part_of_the_bound(
    short: SandboxSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The measurement before the call and the run together end within `call_within`."""
    monkeypatch.setattr(runner, "MEASURE_WITHIN", 1.0)
    limited = dataclasses.replace(short, workspace_limit=MIB)
    sandbox = Sandbox(FakeDocker(late=EVERY_REQUEST), limited)  # type: ignore[arg-type]

    took, outcome = timed_run(sandbox)

    assert measure_within(limited) == 1.0
    run_bound = call_within(limited) - measure_within(limited)
    assert took < run_bound + 0.3
    assert isinstance(outcome, NotStarted), outcome
    assert f"within {run_bound:g} s" in str(outcome)


def test_a_late_daemon_becomes_a_failed_result_that_says_the_command_did_not_run(
    short: SandboxSettings,
) -> None:
    tool = run_shell_tool(Sandbox(FakeDocker(late=EVERY_REQUEST), short))  # type: ignore[arg-type]
    call = ToolCall(Path("/workspaces") / str(JOB), {"command": "true"}, f"{JOB}:toolu_1", 1)

    took, result = timed(lambda: tool.run(call))

    assert took < call_within(short) + 0.3
    assert isinstance(result, ToolResult), result
    assert result.is_error
    assert result.content.startswith("the sandbox could not start the command")


def test_a_late_removal_does_not_hold_the_call_past_its_bound(short: SandboxSettings) -> None:
    docker = FakeDocker(late=frozenset({"remove"}))
    sandbox = Sandbox(docker, short)  # type: ignore[arg-type]

    async def run_and_look() -> tuple[Run, float, list[bool]]:
        began = time.monotonic()
        run = await sandbox.run(JOB, 1, ["true"])
        took = time.monotonic() - began
        # A removal that was cancelled at the bound is done after one more turn of the loop.
        await asyncio.sleep(0.05)
        return run, took, [removal.done() for removal in sandbox._removals]

    _, outcome = timed(run_and_look)

    assert not isinstance(outcome, BaseException), outcome
    run, took, removals_done = outcome
    # The removal had the rest of the bound, the request's 10 s included.
    assert call_within(short) - 0.05 <= took < call_within(short) + 0.3
    # The command ran and its result is kept.
    assert run.exit_code == 0
    assert ("remove", docker.calls[0][1]) in docker.calls
    # The removal goes on without the call: still running, not cancelled.
    assert removals_done == [False]


def test_the_commands_timeout_counts_from_the_start_request(short: SandboxSettings) -> None:
    """A slow set-up does not eat into the command's time."""
    sandbox = Sandbox(FakeDocker(create_takes=0.15, late=frozenset({"wait"})), short)  # type: ignore[arg-type]

    took, outcome = timed_run(sandbox)

    assert isinstance(outcome, Run), outcome
    assert outcome.timed_out
    # 0.15 s to create, then the full 0.2 s timeout.
    assert took >= 0.15 + 0.2


def test_a_wait_that_never_answers_ends_at_the_commands_timeout_within_the_bound(
    short: SandboxSettings,
) -> None:
    sandbox = Sandbox(FakeDocker(late=frozenset({"wait", "remove"})), short)  # type: ignore[arg-type]

    took, outcome = timed_run(sandbox)

    assert took < call_within(short) + 0.3
    # Started, then given up at the command's own timeout: a run without an exit status.
    assert isinstance(outcome, Run), outcome
    assert outcome.timed_out


def test_a_scoped_call_whose_removal_is_late_keeps_its_result_and_loses_only_the_count(
    short: SandboxSettings,
) -> None:
    """The command's container is removed before the guard's count is read, inside the
    bound. The bound runs out there, after the command ended: its output is in hand."""
    scoped = dataclasses.replace(short, guard_image="guard:test", network="sandbox-net")
    docker = FakeDocker(late=frozenset({"remove"}))
    sandbox = Sandbox(docker, scoped)  # type: ignore[arg-type]

    took, outcome = timed_run(sandbox, Resolved(allowed=(), hosts=()))

    assert took < call_within(scoped) + 0.3
    started = [name for call, name in docker.calls if call == "start"]
    assert [name.split("-")[2] for name in started] == ["guard", "sandbox"]
    assert isinstance(outcome, Run), outcome
    assert (outcome.exit_code, outcome.dropped) == (0, None)


@pytest.mark.parametrize("value", ["1\n", "2\n", "3\n"])
def test_a_host_where_a_process_can_trace_only_its_descendants_is_accepted(
    tmp_path: Path, value: str
) -> None:
    scope = tmp_path / "ptrace_scope"
    scope.write_text(value)

    check_host(scope)


@pytest.mark.parametrize("value", ["0\n", "", "x"])
def test_a_host_where_a_command_could_trace_its_timeout_is_refused(
    tmp_path: Path, value: str
) -> None:
    scope = tmp_path / "ptrace_scope"
    scope.write_text(value)

    with pytest.raises(SandboxError, match="ptrace_scope"):
        check_host(scope)


def test_a_host_without_yama_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SandboxError, match="Yama"):
        check_host(tmp_path / "missing")


class Networks:
    """Answers `network_subnets` like Docker for a few named networks."""

    def __init__(self, subnets: dict[str, list[str]]) -> None:
        self.subnets = subnets

    async def network_subnets(self, name: str) -> list[str]:
        return self.subnets[name]


def test_the_deny_list_is_the_fixed_one_plus_every_subnet_of_the_networks() -> None:
    docker = Networks(
        {"sandbox-net": ["172.22.0.0/16"], "stack-net": ["172.21.0.0/16", "fd00::/64"]}
    )

    deny = asyncio.run(deny_list(docker, ("sandbox-net", "stack-net")))  # type: ignore[arg-type]

    assert deny == (
        *FIXED_DENY,
        ipaddress.ip_network("172.22.0.0/16"),
        ipaddress.ip_network("172.21.0.0/16"),
        ipaddress.ip_network("fd00::/64"),
    )


def test_a_subnet_that_cannot_be_read_is_a_sandbox_error() -> None:
    docker = Networks({"odd": ["not-a-subnet"]})

    with pytest.raises(SandboxError, match="cannot be read"):
        asyncio.run(deny_list(docker, ("odd",)))  # type: ignore[arg-type]
