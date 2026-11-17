"""The escape suite's sandbox: the real runner against the Docker daemon.

Needs the sandbox image (`make escape` builds it). No database and no stack.
The job workspaces are directories under the test's temporary directory, and
a named volume is bound onto them, as the stack's workspace volume is.

ESCAPE_DOCKER_DEFAULTS=1 runs the same attempts in containers made with
Docker's default settings, only the workspace mount, and the command as root.
That run must fail: it is the proof that the suite can see an escape.

Commands run as the uid the stack uses, the default of SandboxSettings.
"""

import asyncio
import os
import subprocess
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from runtime.sandbox import runner
from runtime.sandbox.docker import Docker
from runtime.sandbox.runner import LABEL, WORKDIR, Run, Sandbox, SandboxSettings
from runtime.scope import Resolved
from runtime.tools.base import ToolCall, ToolResult
from runtime.tools.run_shell import run_shell_tool

SOCKET = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
IMAGE = os.environ.get("SANDBOX_IMAGE", "agent-runtime-sandbox:dev")
GUARD_IMAGE = os.environ.get("GUARD_IMAGE", "agent-runtime-guard:dev")
DEFAULTS = os.environ.get("ESCAPE_DOCKER_DEFAULTS") == "1"
# The guard sets no rules: every scope test that expects a block must fail.
NO_RULES = os.environ.get("ESCAPE_NO_RULES") == "1"
# A ruleset that allows everything and still has the counter the worker reads.
ALLOW_ALL = """table inet guard {
  counter dropped {
  }
  chain output {
    type filter hook output priority 0; policy accept;
  }
}
"""


def docker_cli(*args: str) -> str:
    return subprocess.run(  # noqa: S603 - fixed command, arguments come from the tests
        ["docker", *args],  # noqa: S607 - the docker CLI from PATH, as in the other suites
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout


def default_config(
    settings: SandboxSettings,
    job_id: uuid.UUID,
    lease_epoch: int,
    argv: Sequence[str],
    *,
    guard: str | None = None,
) -> dict[str, Any]:
    """A container as `docker run --user 0:0` makes it, with only the job's workspace mounted.

    Root, because the sandbox image itself sets a user that is not root: a
    default container from it would have no capabilities and could not
    write to its root file system, and those attempts could not fail.

    For a scoped call it is put on the guard's network directly, as a plain
    `docker run --network` would, instead of joining the guard.
    """
    host_config: dict[str, Any] = {
        "Mounts": [
            {
                "Type": "volume",
                "Source": settings.volume,
                "Target": WORKDIR,
                "VolumeOptions": {"NoCopy": True, "Subpath": str(job_id)},
            }
        ]
    }
    if guard is not None and settings.network is not None:
        host_config["NetworkMode"] = settings.network
    return {
        "Image": settings.image,
        "Cmd": list(argv),
        "WorkingDir": WORKDIR,
        "User": "0:0",
        "Labels": {LABEL: "1"},
        "AttachStdout": True,
        "AttachStderr": True,
        "HostConfig": host_config,
    }


class OwnClientSandbox:
    """A sandbox that opens a Docker client for each call, in the call's own event loop."""

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
            return await Sandbox(docker, self.settings).run(job_id, lease_epoch, argv, scope)
        finally:
            await docker.aclose()

    async def remove_older(self, job_id: uuid.UUID, lease_epoch: int) -> int:
        docker = Docker(SOCKET)
        try:
            return await Sandbox(docker, self.settings).remove_older(job_id, lease_epoch)
        finally:
            await docker.aclose()


@dataclass
class Host:
    """What the attempts run against: two job workspaces and a canary on the host."""

    root: Path
    volume: str
    job: uuid.UUID
    other_job: uuid.UUID
    # A file on the host, outside every workspace. Its name is a token that
    # appears nowhere else, so finding it from inside means it was reachable.
    canary: Path
    other_secret: Path
    settings: SandboxSettings

    def shell(self, script: str, scope: Resolved | None = None) -> ToolResult:
        """Run `script` with run_shell in the job's workspace, as a tool call of the job."""
        return asyncio.run(self.shell_async(script, scope))

    async def shell_async(self, script: str, scope: Resolved | None = None) -> ToolResult:
        tool = run_shell_tool(OwnClientSandbox(self.settings))
        call = ToolCall(
            self.root / str(self.job),
            {"command": script},
            f"{self.job}:toolu_1",
            lease_epoch=1,
            scope=scope,
        )
        return await tool.run(call)


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Host]:
    if DEFAULTS:
        monkeypatch.setattr(runner, "container_config", default_config)
    if NO_RULES:
        monkeypatch.setattr(runner, "ruleset", lambda scope, deny: ALLOW_ALL)
    root = tmp_path / "workspaces"
    root.mkdir()
    job, other_job = uuid.uuid4(), uuid.uuid4()
    # In the stack the worker runs as the sandbox's uid and owns the
    # workspaces. Here this process owns them, so each job's directory is
    # opened to every uid instead.
    for workspace in (job, other_job):
        (root / str(workspace)).mkdir()
        (root / str(workspace)).chmod(0o777)
    other_secret = root / str(other_job) / f"secret-{uuid.uuid4().hex}.txt"
    other_secret.write_text("another job's file\n")
    canary_dir = tmp_path / "host"
    canary_dir.mkdir()
    canary = canary_dir / f"canary-{uuid.uuid4().hex}.txt"
    canary.write_text("host file\n")
    volume = f"escape-workspaces-{uuid.uuid4().hex}"
    docker_cli(
        "volume", "create", "--driver", "local",
        "--opt", "type=none", "--opt", "o=bind", "--opt", f"device={root}",
        volume,
    )  # fmt: skip
    # Guards join a network of this test's own. Its targets live there too,
    # so its subnet is not in the deny list here, as it is in the stack.
    network = f"escape-net-{uuid.uuid4().hex}"
    docker_cli("network", "create", network)
    settings = SandboxSettings(
        image=IMAGE,
        volume=volume,
        timeout=30.0,
        guard_image=GUARD_IMAGE,
        network=network,
    )
    try:
        yield Host(root, volume, job, other_job, canary, other_secret, settings)
    finally:
        for container in (
            docker_cli("ps", "--all", "--quiet", "--filter", f"network={network}").split()
            + docker_cli(
                "ps",
                "--all",
                "--quiet",
                "--filter",
                f"label={LABEL}",
                "--filter",
                f"volume={volume}",
            ).split()
        ):
            docker_cli("rm", "--force", container)
        docker_cli("volume", "rm", "--force", volume)
        docker_cli("network", "rm", network)
