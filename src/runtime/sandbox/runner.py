"""Run one command in one container, for one tool call (ADR 0008).

Every container is built from the same fixed settings. The command is the
only part that comes from the model: never the image, a mount, a user or a
flag. The container gets no network, a read-only root, no capabilities, a
user that is not root, limits on CPU, memory and processes, and one writable
mount: the job's own directory of the workspace volume.
"""

import asyncio
import logging
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from runtime.sandbox.docker import Docker, SandboxError
from runtime.sandbox.output import Output

log = logging.getLogger("runtime.sandbox")

# Where the job's directory appears in the container, and where commands start.
WORKDIR = "/workspace"
# Marks every container the sandbox starts.
LABEL = "agent-runtime.sandbox"
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
    # Bytes of output the result keeps. The rest is counted and dropped.
    output_limit: int = 64 * 1024
    cpus: float = 1.0
    memory: int = 256 * MIB
    pids: int = 64
    # The in-memory /tmp. It counts toward `memory`.
    tmp_size: int = 16 * MIB

    def __post_init__(self) -> None:
        if not self.image:
            raise ValueError("image is empty")
        if not self.volume:
            raise ValueError("volume is empty")
        match = _USER.fullmatch(self.user)
        if match is None or int(match[1]) == 0 or int(match[2]) == 0:
            raise ValueError(f"user must be uid:gid, both numbers above 0, got {self.user!r}")
        # `not value > 0` also refuses NaN.
        if not self.timeout > 0 or math.isinf(self.timeout):
            raise ValueError(f"timeout must be a positive number, got {self.timeout!r}")
        # Docker reads a CPU limit of 0 as no limit, and refuses less than 0.01.
        if not 0.01 <= self.cpus < math.inf:
            raise ValueError(f"cpus must be at least 0.01, got {self.cpus!r}")
        # A tmpfs of size 0 has no size limit.
        if self.tmp_size < MIB:
            raise ValueError(f"tmp_size must be at least 1 MiB, got {self.tmp_size!r}")
        if self.output_limit < 1024:
            # The result's notes about the cut and the exit need room.
            raise ValueError(f"output_limit must be at least 1024, got {self.output_limit!r}")
        for name in ("memory", "pids"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1, got {getattr(self, name)!r}")


def container_config(
    settings: SandboxSettings, job_id: UUID, argv: Sequence[str]
) -> dict[str, Any]:
    """The body of `POST /containers/create` for one call. Only `argv` comes from the model."""
    return {
        "Image": settings.image,
        "Cmd": list(argv),
        "User": settings.user,
        "WorkingDir": WORKDIR,
        "Labels": {LABEL: "1"},
        "NetworkDisabled": True,
        "AttachStdin": False,
        "AttachStdout": True,
        "AttachStderr": True,
        "OpenStdin": False,
        "Tty": False,
        "HostConfig": {
            "NetworkMode": "none",
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "Privileged": False,
            "NanoCpus": round(settings.cpus * 1_000_000_000),
            "Memory": settings.memory,
            "MemorySwap": settings.memory,
            "PidsLimit": settings.pids,
            # A small init as PID 1 reaps the processes the command leaves.
            "Init": True,
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

    @property
    def timed_out(self) -> bool:
        return self.exit_code is None

    @property
    def truncated(self) -> bool:
        return self.total > len(self.output)


class NotStarted(SandboxError):
    """The sandbox failed before it asked Docker to start the command."""


class Sandbox:
    """Runs commands for tool calls. One per worker; it shares the worker's Docker client."""

    def __init__(self, docker: Docker, settings: SandboxSettings) -> None:
        self.docker = docker
        self.settings = settings
        # Removals that go on after the call that started them was cancelled.
        self._removals: set[asyncio.Task[None]] = set()

    async def _remove(self, name: str) -> None:
        try:
            await self.docker.remove(name)
        except SandboxError:
            log.exception("tool container not removed", extra={"container": name})

    async def run(self, job_id: UUID, argv: Sequence[str]) -> Run:
        """Run `argv` in a new container with the job's workspace. Raises SandboxError.

        The container is removed afterwards, also when the call fails or is
        cancelled. A command still running at the timeout is killed.
        """
        settings = self.settings
        # Chosen here, not by Docker: a container whose create was cut off
        # before Docker's answer arrived can still be removed by its name.
        name = f"agent-runtime-sandbox-{uuid4().hex}"
        output = Output(settings.output_limit)
        deadline = asyncio.timeout(settings.timeout)
        exit_code: int | None = None
        started = False
        try:
            await self.docker.create(name, container_config(settings, job_id, argv))
            async with self.docker.attach(name) as stream:
                try:
                    async with deadline:
                        # From here on the command may run, whatever Docker answers.
                        started = True
                        await self.docker.start(name)
                        async for chunk in stream:
                            output.feed(chunk)
                        exit_code = await self.docker.wait(name)
                except TimeoutError:
                    if not deadline.expired():
                        raise
        except SandboxError as error:
            if not started:
                raise NotStarted(str(error)) from error
            raise
        finally:
            # Kills a command that still runs. Shielded, so that a second
            # cancel does not stop the removal half way.
            removal = asyncio.ensure_future(self._remove(name))
            self._removals.add(removal)
            removal.add_done_callback(self._removals.discard)
            await asyncio.shield(removal)
        return Run(bytes(output.kept), output.total, exit_code)
