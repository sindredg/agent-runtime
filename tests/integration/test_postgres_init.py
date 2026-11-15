"""Postgres runs under an init process, which reaps the orphans of health checks.

A health check that runs past its timeout is killed by Docker, and a child it
started lives on. That orphan goes to the container's PID 1. If PID 1 is
Postgres, it takes the orphan's exit for a crashed server process and
restarts every connection (issue 32). Under an init, the init reaps it.

The test starts the stack's postgres service in a project of its own (see
compose.orphans.yml), with a health check that leaves an orphan behind every
time, and reads the Postgres log.
"""

import json
import os
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = Path(__file__).with_name("compose.orphans.yml")
# The final server, not the temporary one the image runs while it sets up
# the database: only the final one listens on TCP.
READY = "listening on IPv4 address"
# What Postgres logs when a child it did not start exits with an error.
RESTART = "untracked child process"
# The process the health check leaves behind, as `ps` shows it. Keep in step
# with compose.orphans.yml.
ORPHAN = "/bin/sh -c sleep 2; exit 2"
# Seconds an orphan lives after its check timed out, with a margin.
ORPHAN_LIFETIME = 3.0
# Orphans that have to be seen under the container's PID 1 after the server
# is ready.
ORPHANS = 3


def _run(*args: str, check: bool = True) -> str:
    env = dict(os.environ)
    # Compose reads the whole stack file, and the worker service needs this.
    env.setdefault("DOCKER_GID", str(Path("/var/run/docker.sock").stat().st_gid))
    done = subprocess.run(  # noqa: S603 - fixed program, arguments come from the test
        args, cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=False, timeout=120
    )
    if check and done.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed:\n{done.stdout}\n{done.stderr}")
    return done.stdout + done.stderr


@pytest.fixture
def compose() -> Iterator[list[str]]:
    """The compose command of a throwaway project, removed with its volumes afterwards."""
    command = [
        "docker",
        "compose",
        "-f",
        str(COMPOSE_FILE),
        "-p",
        f"agent-runtime-orphans-{uuid.uuid4().hex[:8]}",
    ]
    try:
        yield command
    finally:
        # Not checked: a failed cleanup must not hide the test's own result.
        _run(*command, "down", "--volumes", "--timeout", "10", check=False)


def _wait_for(what: str, ready: Callable[[], bool], timeout: float) -> None:
    end = time.monotonic() + timeout
    while not ready():
        if time.monotonic() > end:
            raise AssertionError(f"{what} not seen within {timeout:g} s")
        time.sleep(0.25)


def test_an_orphaned_health_check_does_not_restart_postgres(compose: list[str]) -> None:
    _run(*compose, "up", "--detach", "postgres")
    container = _run(*compose, "ps", "--all", "--quiet", "postgres").strip()

    def logs() -> str:
        return _run(*compose, "logs", "--no-color", "postgres")

    def state() -> dict[str, Any]:
        found: dict[str, Any] = json.loads(
            _run("docker", "inspect", "--format", "{{json .State}}", container)
        )
        return found

    orphans: set[int] = set()

    def orphans_seen() -> bool:
        """Note each orphan whose parent is the container's PID 1, by host PID."""
        pid1 = state()["Pid"]
        top = _run("docker", "top", container, "-o", "pid,ppid,args", check=False)
        for line in top.splitlines()[1:]:
            pid, ppid, args = line.split(maxsplit=2)
            if args == ORPHAN and int(ppid) == pid1:
                orphans.add(int(pid))
        return len(orphans) >= ORPHANS

    _wait_for("the server", lambda: READY in logs(), timeout=90)
    # Shows that the check leaves orphans to PID 1. Otherwise a pass proves nothing.
    _wait_for(f"{ORPHANS} orphans under PID 1", orphans_seen, timeout=60)
    time.sleep(ORPHAN_LIFETIME)

    now = state()
    assert now["Running"], now
    restarts = [line for line in logs().splitlines() if RESTART in line]
    assert restarts == [], "\n".join(restarts)
