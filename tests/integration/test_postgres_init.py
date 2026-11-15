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
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = Path(__file__).with_name("compose.orphans.yml")
# The final server, not the temporary one the image runs while it sets up
# the database: only the final one listens on TCP.
READY = "listening on IPv4 address"
# What Postgres logs when a child it did not start exits with an error.
RESTART = "untracked child process"
# Health checks that time out after the server is ready. Each leaves an
# orphan that exits a second later.
ORPHANS = 4


def _run(*args: str) -> str:
    env = dict(os.environ)
    # Compose reads the whole stack file, and the worker service needs this.
    env.setdefault("DOCKER_GID", str(Path("/var/run/docker.sock").stat().st_gid))
    done = subprocess.run(  # noqa: S603 - fixed program, arguments come from the test
        args, cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=False, timeout=120
    )
    if done.returncode != 0:
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
        _run(*command, "down", "--volumes", "--timeout", "10")


def _wait_for(what: str, ready: object, timeout: float) -> None:
    end = time.monotonic() + timeout
    while not ready():  # type: ignore[operator]
        if time.monotonic() > end:
            raise AssertionError(f"{what} not seen within {timeout:g} s")
        time.sleep(0.5)


def test_an_orphaned_health_check_does_not_restart_postgres(compose: list[str]) -> None:
    _run(*compose, "up", "--detach", "postgres")
    container = _run(*compose, "ps", "--all", "--quiet", "postgres").strip()

    def logs() -> str:
        return _run(*compose, "logs", "--no-color", "postgres")

    def failing_streak() -> int:
        state = json.loads(_run("docker", "inspect", "--format", "{{json .State}}", container))
        return int(state["Health"]["FailingStreak"])

    _wait_for("the server", lambda: READY in logs(), timeout=90)
    before = failing_streak()
    _wait_for(
        f"{ORPHANS} timed out health checks",
        lambda: failing_streak() >= before + ORPHANS,
        timeout=60,
    )
    # The last orphan exits a second after its check timed out.
    time.sleep(3)

    state = json.loads(_run("docker", "inspect", "--format", "{{json .State}}", container))
    assert state["Running"], state
    last = state["Health"]["Log"][-1]
    # Every check timed out, so every check left an orphan.
    assert "exceeded timeout" in last["Output"], last
    restarts = [line for line in logs().splitlines() if RESTART in line]
    assert restarts == [], "\n".join(restarts)
