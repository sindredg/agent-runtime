"""The escape suite's sandbox: the real runner against the Docker daemon.

Needs the sandbox image (`make escape` builds it). No database and no stack.
The job workspaces are directories under the test's temporary directory, and
a named volume is bound onto them, as the stack's workspace volume is.

ESCAPE_DOCKER_DEFAULTS=1 runs the same attempts in containers made with
Docker's default settings and only the workspace mount. That run must fail:
it is the proof that the suite can see an escape.
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
from runtime.tools.base import ToolCall, ToolResult
from runtime.tools.run_shell import run_shell_tool

SOCKET = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
IMAGE = os.environ.get("SANDBOX_IMAGE", "agent-runtime-sandbox:dev")
DEFAULTS = os.environ.get("ESCAPE_DOCKER_DEFAULTS") == "1"


def docker_cli(*args: str) -> str:
    return subprocess.run(  # noqa: S603 - fixed command, arguments come from the tests
        ["docker", *args],  # noqa: S607 - the docker CLI from PATH, as in the other suites
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout


def default_config(
    settings: SandboxSettings, job_id: uuid.UUID, argv: Sequence[str]
) -> dict[str, Any]:
    """A container as `docker run` makes it, with only the job's workspace mounted."""
    return {
        "Image": settings.image,
        "Cmd": list(argv),
        "WorkingDir": WORKDIR,
        "Labels": {LABEL: "1"},
        "AttachStdout": True,
        "AttachStderr": True,
        "HostConfig": {
            "Mounts": [
                {
                    "Type": "volume",
                    "Source": settings.volume,
                    "Target": WORKDIR,
                    "VolumeOptions": {"NoCopy": True, "Subpath": str(job_id)},
                }
            ]
        },
    }


class OwnClientSandbox:
    """A sandbox that opens a Docker client for each call, in the call's own event loop."""

    def __init__(self, settings: SandboxSettings) -> None:
        self.settings = settings

    async def run(self, job_id: uuid.UUID, argv: Sequence[str]) -> Run:
        docker = Docker(SOCKET)
        try:
            return await Sandbox(docker, self.settings).run(job_id, argv)
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

    def shell(self, script: str) -> ToolResult:
        """Run `script` with run_shell in the job's workspace, as a tool call of the job."""
        tool = run_shell_tool(OwnClientSandbox(self.settings))
        call = ToolCall(self.root / str(self.job), {"command": script}, f"{self.job}:toolu_1")
        return asyncio.run(tool.run(call))


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Host]:
    if DEFAULTS:
        monkeypatch.setattr(runner, "container_config", default_config)
    root = tmp_path / "workspaces"
    root.mkdir()
    job, other_job = uuid.uuid4(), uuid.uuid4()
    (root / str(job)).mkdir()
    (root / str(other_job)).mkdir()
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
    settings = SandboxSettings(
        image=IMAGE, volume=volume, user=f"{os.getuid()}:{os.getgid()}", timeout=30.0
    )
    try:
        yield Host(root, volume, job, other_job, canary, other_secret, settings)
    finally:
        for container in docker_cli(
            "ps", "--all", "--quiet", "--filter", f"label={LABEL}", "--filter", f"volume={volume}"
        ).split():
            docker_cli("rm", "--force", container)
        docker_cli("volume", "rm", "--force", volume)
