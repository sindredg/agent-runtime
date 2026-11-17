"""The check that keeps the integration and chaos suites off the Claude API.

The script runs against a stand-in `docker` on PATH. Each worker is a
directory; its `url` file is what `printenv ANTHROPIC_BASE_URL` prints, and
no file means the variable is unset.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_fake_provider.py"
FAKE = "http://fake-provider:8000"
API = "https://api.anthropic.com"

DOCKER = """#!/bin/sh
echo "$*" >> "$FAKE_DOCKER/calls"
case "$1" in
  ps)
    if [ -f "$FAKE_DOCKER/ps-fails" ]; then echo "daemon down" >&2; exit 1; fi
    ls "$FAKE_DOCKER/workers" ;;
  exec)
    [ "$3 $4" = "printenv ANTHROPIC_BASE_URL" ] || exit 2
    cat "$FAKE_DOCKER/workers/$2/url" 2>/dev/null || exit 1 ;;
esac
"""


class Stack:
    def __init__(self, root: Path) -> None:
        self.root = root
        (root / "bin").mkdir()
        (root / "workers").mkdir()
        docker = root / "bin" / "docker"
        docker.write_text(DOCKER)
        docker.chmod(0o755)

    def worker(self, name: str, url: str | None) -> None:
        (self.root / "workers" / name).mkdir()
        if url is not None:
            (self.root / "workers" / name / "url").write_text(url + "\n")

    def check(self) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "PATH": f"{self.root / 'bin'}:{os.environ['PATH']}",
            "FAKE_DOCKER": str(self.root),
        }
        return subprocess.run(  # noqa: S603 - fixed program and script
            [sys.executable, str(SCRIPT)], capture_output=True, text=True, env=env, check=False
        )

    def calls(self) -> list[str]:
        return (self.root / "calls").read_text().splitlines()


@pytest.fixture
def stack(tmp_path: Path) -> Stack:
    return Stack(tmp_path)


def test_every_worker_on_the_fake_passes(stack: Stack) -> None:
    stack.worker("agent-runtime-worker-1", FAKE)
    stack.worker("agent-runtime-worker-2", FAKE)
    done = stack.check()
    assert (done.returncode, done.stderr) == (0, "")


def test_one_worker_on_the_api_is_named(stack: Stack) -> None:
    stack.worker("agent-runtime-worker-1", FAKE)
    stack.worker("agent-runtime-worker-2", API)
    done = stack.check()
    assert done.returncode == 1
    assert f"agent-runtime-worker-2 calls {API}" in done.stderr
    assert "worker-1" not in done.stderr


def test_an_unset_url_fails(stack: Stack) -> None:
    """Without ANTHROPIC_BASE_URL the worker calls the Claude API."""
    stack.worker("agent-runtime-worker-1", None)
    done = stack.check()
    assert done.returncode == 1
    assert "agent-runtime-worker-1 has no ANTHROPIC_BASE_URL" in done.stderr


def test_no_worker_fails(stack: Stack) -> None:
    done = stack.check()
    assert done.returncode == 1
    assert "no worker is running" in done.stderr


def test_a_failed_docker_ps_fails(stack: Stack) -> None:
    (stack.root / "ps-fails").touch()
    done = stack.check()
    assert done.returncode == 1
    assert "daemon down" in done.stderr


def test_only_the_url_is_read(stack: Stack) -> None:
    """With the stack on the API, the key is in the worker's environment."""
    stack.worker("agent-runtime-worker-1", FAKE)
    stack.check()
    execs = [call for call in stack.calls() if call.startswith("exec")]
    assert execs == ["exec agent-runtime-worker-1 printenv ANTHROPIC_BASE_URL"]
