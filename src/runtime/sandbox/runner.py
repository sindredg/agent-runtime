"""Run one command in one container, for one tool call (ADR 0008).

Every container is built from the same fixed settings. The command is the
only part that comes from the model: never the image, a mount, a user or a
flag. The container gets no network, a read-only root, no capabilities, a
user that is not root, limits on CPU, memory and processes, and one writable
mount: the job's own directory of the workspace volume.

A container can outlive the worker that started it. It ends by itself: the
command runs under `timeout` inside the container, so it is killed even when
no worker is left to do it. Each container carries its job id and lease
epoch, so that the worker that takes the job over can remove it.
"""

import asyncio
import contextlib
import ipaddress
import logging
import math
import re
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from runtime.sandbox.docker import REQUEST_TIMEOUT, Docker, SandboxError
from runtime.sandbox.output import Output
from runtime.scope import FIXED_DENY, Network, Resolved, parse_dropped, ruleset

log = logging.getLogger("runtime.sandbox")

# Where the job's directory appears in the container, and where commands start.
WORKDIR = "/workspace"
# Marks every container the sandbox starts.
LABEL = "agent-runtime.sandbox"
# The job the container runs a call of, and the lease epoch of the worker
# that started it.
JOB_LABEL = "agent-runtime.job"
EPOCH_LABEL = "agent-runtime.lease-epoch"
# GNU coreutils in the sandbox image. It kills the command at its timeout
# also when the worker that started it is gone.
TIMEOUT_PROGRAM = "/usr/bin/timeout"
# Seconds the timeout inside the container waits beyond the worker's own, so
# that a worker that is still there stops the command first and reports it.
KILL_MARGIN = 2.0
# The longest timeout or start bound accepted: one day.
MAX_SECONDS = 86_400.0
MIB = 1024 * 1024
_USER = re.compile(r"([0-9]+):([0-9]+)")


@dataclass(frozen=True, slots=True)
class SandboxSettings:
    # The image every tool container runs. It is not pulled: it must exist.
    image: str
    # The named volume that holds the job workspaces, one directory per job.
    volume: str
    # uid:gid, both numbers, neither 0. It owns the files the command writes,
    # so it is the uid the worker runs as.
    user: str = "10001:10001"
    # Seconds the command may run. Then the container is killed.
    timeout: float = 30.0
    # Seconds from the start of a call after which Docker is no longer asked
    # to start the command. Bounds when a container of a stopped worker can
    # still begin to run (see `ends_within`).
    start_within: float = 15.0
    # Bytes of output the result keeps. The rest is counted and dropped.
    output_limit: int = 64 * 1024
    cpus: float = 1.0
    memory: int = 256 * MIB
    pids: int = 64
    # The in-memory /tmp. It counts toward `memory`.
    tmp_size: int = 16 * MIB
    # For a job with a scope (ADR 0009): the image of the guard container
    # that holds the firewall, and the Docker network it joins. Both unset:
    # no call can be scoped.
    guard_image: str | None = None
    network: str | None = None
    # Addresses no scope can open. Always holds FIXED_DENY.
    deny: tuple[Network, ...] = FIXED_DENY

    def __post_init__(self) -> None:
        if not self.image:
            raise ValueError("image is empty")
        if not self.volume:
            raise ValueError("volume is empty")
        match = _USER.fullmatch(self.user)
        if match is None or int(match[1]) == 0 or int(match[2]) == 0:
            raise ValueError(f"user must be uid:gid, both numbers above 0, got {self.user!r}")
        for name in ("timeout", "start_within"):
            value = getattr(self, name)
            # `not value > 0` also refuses NaN.
            if not value > 0 or value > MAX_SECONDS:
                raise ValueError(
                    f"{name} must be above 0 and at most {MAX_SECONDS:g}, got {value!r}"
                )
        # Docker reads a CPU limit of 0 as no limit, and refuses less than 0.01.
        if not 0.01 <= self.cpus < math.inf:
            raise ValueError(f"cpus must be at least 0.01, got {self.cpus!r}")
        # A tmpfs of size 0 has no size limit.
        if self.tmp_size < MIB:
            raise ValueError(f"tmp_size must be at least 1 MiB, got {self.tmp_size!r}")
        if self.output_limit < 1024:
            # The result's notes about the cut and the exit need room.
            raise ValueError(f"output_limit must be at least 1024, got {self.output_limit!r}")
        if (self.guard_image is None) != (self.network is None):
            raise ValueError("guard_image and network are set together or not at all")
        if not set(FIXED_DENY) <= set(self.deny):
            raise ValueError("deny must hold the fixed deny list")
        for name in ("memory", "pids"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1, got {getattr(self, name)!r}")


def ends_within(settings: SandboxSettings) -> float:
    """Seconds after a call began by which its container has ended, whatever the worker did.

    The worker does not ask Docker to start the command after `start_within`,
    and that request ends within its own timeout. From the start the command
    has the timeout inside the container. Not covered: a worker that is
    paused between the start of the call and the start request, and a
    daemon that acts on a request after the worker gave up on it.
    """
    return settings.start_within + REQUEST_TIMEOUT + settings.timeout + KILL_MARGIN


# The longest `ends_within` any valid settings give.
LONGEST_BOUND = 2 * MAX_SECONDS + REQUEST_TIMEOUT + KILL_MARGIN


# What a guard runs: set the rules, say so with the call's own token, wait.
# On TERM it prints the count of dropped packets and ends. Fixed text: the
# scope reaches it only as the rules in RULES, built from addresses and ports.
# The token is random per call, so no output of nft can pass for it.
GUARD_PROGRAM = """set -eu
printf '%s' "$RULES" | nft -f -
trap 'nft list counter inet guard dropped; exit 0' TERM
echo "ready $READY"
sleep "$LIFETIME" &
wait $!
"""


def _labels(job_id: UUID, lease_epoch: int) -> dict[str, str]:
    return {LABEL: "1", JOB_LABEL: str(job_id), EPOCH_LABEL: str(lease_epoch)}


def guard_config(
    settings: SandboxSettings, job_id: UUID, lease_epoch: int, scope: Resolved, ready: str
) -> dict[str, Any]:
    """The body of `POST /containers/create` for a scoped call's guard.

    It sets the rules in its network namespace and waits. It is root with
    NET_ADMIN only, in a namespace of its own; the command never runs in it.
    The scope reaches it as rules built from addresses and ports, and as
    pinned host names.
    """
    assert settings.guard_image is not None and settings.network is not None  # noqa: S101
    return {
        "Image": settings.guard_image,
        "Cmd": ["/bin/sh", "-c", GUARD_PROGRAM],
        "Env": [
            f"RULES={ruleset(scope, settings.deny)}",
            f"READY={ready}",
            # It goes away by itself, at the latest when the command must have.
            f"LIFETIME={math.ceil(ends_within(settings))}",
        ],
        "Labels": _labels(job_id, lease_epoch),
        "AttachStdin": False,
        "AttachStdout": True,
        "AttachStderr": True,
        "OpenStdin": False,
        "Tty": False,
        "HostConfig": {
            "NetworkMode": settings.network,
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "CapAdd": ["NET_ADMIN"],
            "SecurityOpt": ["no-new-privileges:true"],
            "Privileged": False,
            "NanoCpus": 250_000_000,
            "Memory": 32 * MIB,
            "MemorySwap": 32 * MIB,
            "PidsLimit": 16,
            "Init": False,
            "IpcMode": "private",
            "LogConfig": {"Type": "none", "Config": {}},
            # The command shares this namespace and its hosts file: names in
            # the scope resolve to what the worker resolved, and nothing else.
            "ExtraHosts": [
                f"{name}:{address}" for name, addresses in scope.hosts for address in addresses
            ],
        },
    }


def container_config(
    settings: SandboxSettings,
    job_id: UUID,
    lease_epoch: int,
    argv: Sequence[str],
    *,
    guard: str | None = None,
) -> dict[str, Any]:
    """The body of `POST /containers/create` for one call. Only `argv` comes from the model.

    With `guard`, the command joins that guard's network namespace and gets
    what its rules allow. Without, it has no network.
    """
    return {
        "Image": settings.image,
        # `--` ends timeout's options, so the command cannot add any.
        "Cmd": [
            TIMEOUT_PROGRAM,
            "--signal=KILL",
            "--",
            f"{settings.timeout + KILL_MARGIN:g}",
            *argv,
        ],
        "User": settings.user,
        "WorkingDir": WORKDIR,
        "Labels": _labels(job_id, lease_epoch),
        "NetworkDisabled": guard is None,
        "AttachStdin": False,
        "AttachStdout": True,
        "AttachStderr": True,
        "OpenStdin": False,
        "Tty": False,
        "HostConfig": {
            "NetworkMode": "none" if guard is None else f"container:{guard}",
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "Privileged": False,
            "NanoCpus": round(settings.cpus * 1_000_000_000),
            "Memory": settings.memory,
            "MemorySwap": settings.memory,
            "PidsLimit": settings.pids,
            # No init: `timeout` is PID 1. The kernel does not deliver a
            # signal it has no handler for, SIGSTOP and SIGKILL among them, to
            # the init of a namespace from inside it, so the command cannot
            # stop its own timeout (ptrace is covered by `check_host`). When
            # `timeout` ends, every process left in the container ends with
            # it. The cost: nothing reaps orphaned processes during the call,
            # and they count toward `pids`.
            "Init": False,
            "IpcMode": "private",
            "LogConfig": {"Type": "none", "Config": {}},
            # In the container's memory, not on the host.
            "Tmpfs": {"/tmp": f"rw,noexec,nosuid,nodev,size={settings.tmp_size // MIB}m"},  # noqa: S108
            "Mounts": [
                {
                    "Type": "volume",
                    "Source": settings.volume,
                    "Target": WORKDIR,
                    "ReadOnly": False,
                    # The job id is a UUID, so the subpath cannot name
                    # anything but that job's directory.
                    "VolumeOptions": {"NoCopy": True, "Subpath": str(job_id)},
                }
            ],
        },
    }


@dataclass(frozen=True, slots=True)
class Run:
    """What one command did."""

    # The first `output_limit` bytes of stdout and stderr, in arrival order.
    output: bytes
    # Bytes the command wrote in all.
    total: int
    # None when the command was killed at its timeout.
    exit_code: int | None
    # For a scoped call: packets the guard dropped, to the deny list or
    # outside the scope. None without a scope, or when the count was lost.
    dropped: int | None = None

    @property
    def timed_out(self) -> bool:
        return self.exit_code is None

    @property
    def truncated(self) -> bool:
        return self.total > len(self.output)


# Yama's ptrace setting of the host kernel. Not namespaced: a container reads the host's.
PTRACE_SCOPE = Path("/proc/sys/kernel/yama/ptrace_scope")


async def deny_list(docker: Docker, networks: Sequence[str]) -> tuple[Network, ...]:
    """The fixed deny list plus every subnet of these Docker networks. Raises SandboxError."""
    deny = list(FIXED_DENY)
    for name in networks:
        for subnet in await docker.network_subnets(name):
            try:
                network = ipaddress.ip_network(subnet, strict=False)
            except ValueError as error:
                raise SandboxError(f"network {name!r} has a subnet that cannot be read") from error
            if network not in deny:
                deny.append(network)
    return tuple(deny)


def check_host(ptrace_scope: Path = PTRACE_SCOPE) -> None:
    """Refuse a host on which a command could stop its container's timeout.

    `timeout` is PID 1 and runs as the command's uid. Signals cannot stop it
    (see `container_config`), but ptrace could: an attach stops the traced
    process. With Yama's ptrace_scope at 1 or above a process may only trace
    its own descendants, and PID 1 is not one. At 0, or without Yama, it may
    trace any process of its uid. Raises SandboxError then.
    """
    try:
        value = ptrace_scope.read_text().strip()
    except OSError as error:
        raise SandboxError(
            f"cannot read {ptrace_scope} ({error.strerror}): the sandbox needs the Yama"
            " LSM with kernel.yama.ptrace_scope at 1 or above"
        ) from error
    if value not in {"1", "2", "3"}:
        raise SandboxError(
            f"kernel.yama.ptrace_scope is {value!r}: the sandbox needs 1 or above, or a"
            " command could stop its own timeout with ptrace"
        )


class NotStarted(SandboxError):
    """The sandbox failed before it asked Docker to start the command."""


def _epoch(container: dict[str, Any]) -> int | None:
    labels = container.get("Labels")
    value = labels.get(EPOCH_LABEL) if isinstance(labels, dict) else None
    try:
        return int(value) if isinstance(value, str) else None
    except ValueError:
        return None


class Sandbox:
    """Runs commands for tool calls. One per worker; it shares the worker's Docker client."""

    def __init__(self, docker: Docker, settings: SandboxSettings) -> None:
        self.docker = docker
        self.settings = settings
        # Removals that go on after the call that started them was cancelled.
        self._removals: set[asyncio.Task[bool]] = set()

    async def _remove(self, name: str) -> bool:
        """Remove a container. False, and logged, when that failed."""
        try:
            await self.docker.remove(name)
        except SandboxError:
            log.exception("tool container not removed", extra={"container": name})
            return False
        return True

    async def remove_older(self, job_id: UUID, lease_epoch: int) -> int:
        """Remove the job's containers that a worker with an earlier lease epoch started.

        Running ones are killed. Returns how many were removed. A container
        whose epoch cannot be read is removed too: it is not this worker's.
        One that cannot be removed does not stop the others. Raises
        SandboxError when the list cannot be read or any removal failed.
        """
        found = await self.docker.containers({LABEL: "1", JOB_LABEL: str(job_id)})
        removed, failed = 0, []
        for container in found:
            epoch = _epoch(container)
            if epoch is not None and epoch >= lease_epoch:
                continue
            try:
                await self.docker.remove(str(container["Id"]))
            except SandboxError as error:
                failed.append(f"{container['Id']}: {error}")
                continue
            removed += 1
        if failed:
            raise SandboxError(f"{len(failed)} container(s) not removed: {'; '.join(failed)}")
        return removed

    async def _start(self, name: str) -> None:
        """Start a container. The whole request is bounded, not each phase of it."""
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                await self.docker.start(name)
        except TimeoutError as error:
            raise SandboxError(
                f"Docker did not answer the start in {REQUEST_TIMEOUT:g} s"
            ) from error

    async def _guard_ready(
        self, name: str, stream: AsyncIterator[bytes], start_by: float, token: str
    ) -> None:
        """Start the guard and wait until its rules are in place, at the latest by `start_by`."""
        said = Output(64 * 1024)
        marker = f"ready {token}\n".encode()
        try:
            async with asyncio.timeout_at(start_by):
                await self._start(name)
                async for chunk in stream:
                    said.feed(chunk)
                    if marker in said.kept:
                        return
        except TimeoutError as error:
            raise SandboxError("the guard was not ready in time") from error
        text = bytes(said.kept).decode("utf-8", "replace")
        raise SandboxError(f"the guard ended before its rules were in place: {text[:500]}")

    async def _dropped(self, name: str, stream: AsyncIterator[bytes]) -> int | None:
        """Stop the guard and read its count of dropped packets. None if it cannot be had."""
        said = Output(4096)
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                await self.docker.kill(name, "TERM")
                async for chunk in stream:
                    said.feed(chunk)
        except SandboxError, TimeoutError:
            log.exception("guard count not read", extra={"container": name})
            return None
        return parse_dropped(bytes(said.kept).decode("utf-8", "replace"))

    def _remove_later(self, name: str) -> asyncio.Future[bool]:
        removal = asyncio.ensure_future(self._remove(name))
        self._removals.add(removal)
        removal.add_done_callback(self._removals.discard)
        return removal

    async def run(
        self,
        job_id: UUID,
        lease_epoch: int,
        argv: Sequence[str],
        scope: Resolved | None = None,
    ) -> Run:
        """Run `argv` in a new container with the job's workspace. Raises SandboxError.

        Without `scope` the command has no network. With it, a guard
        container holds the rules for the scope, and the command joins its
        network namespace (ADR 0009).

        The containers are removed afterwards, also when the call fails or
        is cancelled. A command still running at the timeout is killed.
        Docker is not asked to start it once `start_within` has passed.
        """
        settings = self.settings
        if scope is not None and settings.guard_image is None:
            raise NotStarted("this sandbox has no guard image, so it cannot enforce a scope")
        start_by = asyncio.get_running_loop().time() + settings.start_within
        # Chosen here, not by Docker: a container whose create was cut off
        # before Docker's answer arrived can still be removed by its name.
        name = f"agent-runtime-sandbox-{uuid4().hex}"
        guard = f"agent-runtime-guard-{uuid4().hex}" if scope is not None else None
        token = uuid4().hex
        output = Output(settings.output_limit)
        deadline = asyncio.timeout(settings.timeout)
        exit_code: int | None = None
        dropped: int | None = None
        started = False
        try:
            async with contextlib.AsyncExitStack() as guarded:
                guard_stream = None
                if guard is not None and scope is not None:
                    await self.docker.create(
                        guard, guard_config(settings, job_id, lease_epoch, scope, token)
                    )
                    guard_stream = await guarded.enter_async_context(self.docker.attach(guard))
                    await self._guard_ready(guard, guard_stream, start_by, token)
                await self.docker.create(
                    name, container_config(settings, job_id, lease_epoch, argv, guard=guard)
                )
                async with self.docker.attach(name) as stream:
                    if asyncio.get_running_loop().time() > start_by:
                        raise SandboxError(
                            f"Docker took more than {settings.start_within:g} s"
                            " to set up the container"
                        )
                    try:
                        async with deadline:
                            # From here on the command may run, whatever Docker answers.
                            started = True
                            await self._start(name)
                            async for chunk in stream:
                                output.feed(chunk)
                            exit_code = await self.docker.wait(name)
                    except TimeoutError:
                        if not deadline.expired():
                            raise
                # The command's container goes first, so nothing is sent after
                # the count is read. If it could not be removed, the count
                # would not be the whole story, so there is none.
                if (
                    guard is not None
                    and guard_stream is not None
                    and await self._remove_later(name)
                ):
                    dropped = await self._dropped(guard, guard_stream)
        except SandboxError as error:
            if not started:
                raise NotStarted(str(error)) from error
            raise
        finally:
            # Kills a command that still runs, then its guard. Shielded, so
            # that a second cancel does not stop the removal half way.
            removals = [self._remove_later(name)]
            if guard is not None:
                removals.append(self._remove_later(guard))
            await asyncio.shield(asyncio.gather(*removals))
        return Run(bytes(output.kept), output.total, exit_code, dropped)
