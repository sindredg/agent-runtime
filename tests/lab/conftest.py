"""The lab demo's stack: the compose stack with Gitea, set up from scratch or reused.

`make lab` builds the images and runs these tests. The setup is idempotent:
a second run finds the users and the repository and makes a new token.

What happened is read from Gitea with the admin login, not through the
runtime's tracker code, and from the workers' own logs.
"""

import os
import secrets
import subprocess
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx2
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ["docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.lab.yml"]
GITEA_URL = f"http://127.0.0.1:{os.environ.get('LAB_GITEA_PORT', '3000')}"
API_URL = os.environ.get("RUNTIME_API_URL", "http://127.0.0.1:8000")
DATABASE_URL = os.environ.get(
    "RUNTIME_DATABASE_URL", "postgresql://runtime_app:runtime-app-dev@127.0.0.1:5432/runtime"
)
# The repository owner, who also reads the issues back, and the user the
# worker's token belongs to. The names match docker-compose.lab.yml.
ADMIN, BOT, REPO = "lab-admin", "lab-bot", "shop"
ADMIN_PASSWORD = os.environ.get("LAB_GITEA_PASSWORD", "lab-admin-dev")
TOKEN_NAME = "lab-demo"  # noqa: S105 - the token's name, not a secret
WORKERS = 2
# What a worker logs when it stops at a failpoint.
FIRED = "failpoint reached, stopping the worker"
FINAL = ("succeeded", "failed", "cancelled")


def run(*args: str, env: dict[str, str] | None = None, check: bool = True) -> str:
    """Run a command in the repository and return its output, standard error included."""
    done = subprocess.run(  # noqa: S603 - fixed program, arguments come from the tests
        args,
        cwd=REPO_ROOT,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    if check and done.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:6])} ... failed:\n{done.stdout}\n{done.stderr}")
    return done.stdout + done.stderr


def say(text: str) -> None:
    print(f"  {text}", flush=True)


class Lab:
    def __init__(self) -> None:
        self.api = httpx2.Client(base_url=API_URL, timeout=10)
        # The admin reads Gitea's list for the record. The worker never uses this login.
        self.gitea = httpx2.Client(
            base_url=f"{GITEA_URL}/api/v1", auth=(ADMIN, ADMIN_PASSWORD), timeout=10
        )

    # Setup

    def gitea_up(self) -> None:
        run(*COMPOSE, "up", "--detach", "--wait", "gitea")

    def user(self, name: str, password: str, *, admin: bool) -> None:
        """Make the user if it is missing, and set its password either way."""
        gitea = [*COMPOSE, "exec", "-T", "gitea", "gitea", "admin", "user"]
        found = run(*gitea, "list", check=True)
        if not any(line.split()[1:2] == [name] for line in found.splitlines()[1:]):
            run(*gitea, "create", "--username", name, "--password", password,
                "--email", f"{name}@lab.invalid", "--must-change-password=false",
                *(["--admin"] if admin else []))  # fmt: skip
        run(*gitea, "change-password", "--username", name, "--password", password,
            "--must-change-password=false")  # fmt: skip

    def repository(self) -> None:
        response = self.gitea.post("/user/repos", json={"name": REPO, "private": False})
        if response.status_code not in (201, 409):
            raise RuntimeError(f"creating the repository: {response.status_code} {response.text}")

    def new_token(self, bot_password: str) -> str:
        """A new token for the bot. It can write issues and read its own user."""
        bot = httpx2.Client(base_url=f"{GITEA_URL}/api/v1", auth=(BOT, bot_password), timeout=10)
        with bot:
            gone = bot.delete(f"/users/{BOT}/tokens/{TOKEN_NAME}")
            if gone.status_code not in (204, 404):
                raise RuntimeError(f"removing the old token: {gone.status_code}")
            made = bot.post(
                f"/users/{BOT}/tokens",
                json={"name": TOKEN_NAME, "scopes": ["write:issue", "read:user"]},
            ).raise_for_status()
        token: str = made.json()["sha1"]
        return token

    def set_up(self) -> None:
        self.gitea_up()
        self.user(ADMIN, ADMIN_PASSWORD, admin=True)
        # The bot's password is only used to make its token, so a new one each run.
        bot_password = secrets.token_urlsafe(24)
        self.user(BOT, bot_password, admin=False)
        self.repository()
        # In the process environment, which compose inherits. Never an
        # argument of run(): pytest prints a failing call's arguments.
        os.environ["LAB_GITEA_TOKEN"] = self.new_token(bot_password)

    def workers_up(self, failpoint: str = "") -> None:
        """The whole stack, with the workers stopping at `failpoint`. Matching containers stay."""
        run(*COMPOSE, "up", "--detach", "--wait", "--no-build", "--scale", f"worker={WORKERS}",
            env={"FAILPOINT": failpoint})  # fmt: skip
        for container in run(*COMPOSE, "ps", "--quiet", "worker").split():
            provider = run("docker", "exec", container, "printenv", "ANTHROPIC_BASE_URL").strip()
            if provider != "http://fake-provider:8000":
                raise RuntimeError(f"a worker is not on the fake provider: {provider!r}")

    # Reading what happened

    def worker_logs(self, since: datetime) -> str:
        containers = run(*COMPOSE, "ps", "--all", "--quiet", "worker").split()
        return "".join(
            run("docker", "logs", "--since", since.isoformat(), c, check=False) for c in containers
        )

    def submit(self, job: dict[str, Any]) -> str:
        job_id: str = self.api.post("/jobs", json=job).raise_for_status().json()["id"]
        return job_id

    def job(self, job_id: str) -> dict[str, Any]:
        found: dict[str, Any] = self.api.get(f"/jobs/{job_id}").raise_for_status().json()
        return found

    def steps(self, job_id: str) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = (
            self.api.get(f"/jobs/{job_id}/steps").raise_for_status().json()["steps"]
        )
        return found

    def finish(self, job_id: str, timeout: float = 90.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while (job := self.job(job_id))["status"] not in FINAL:
            if time.monotonic() > deadline:
                raise AssertionError(f"job {job_id} was not final after {timeout:g} s: {job}")
            time.sleep(0.2)
        return job

    def issues(self) -> list[dict[str, Any]]:
        """Every issue in the repository, by anyone, newest first."""
        found: list[dict[str, Any]] = []
        page = 1
        while True:
            params: dict[str, str | int] = {
                "state": "all",
                "type": "issues",
                "limit": 50,
                "page": page,
            }
            response = self.gitea.get(f"/repos/{ADMIN}/{REPO}/issues", params=params)
            entries = response.raise_for_status().json()
            found += entries
            if len(entries) < 50:
                return found
            page += 1

    def last_issue_number(self) -> int:
        return max((issue["number"] for issue in self.issues()), default=0)

    def issues_after(self, number: int) -> list[dict[str, Any]]:
        """The issues opened after issue `number`. Read without the marker the tool writes."""
        return [issue for issue in self.issues() if issue["number"] > number]


@pytest.fixture(scope="session")
def lab() -> Iterator[Lab]:
    print(f"\nSetting up Gitea at {GITEA_URL} ({ADMIN}/{REPO}, token for {BOT})", flush=True)
    lab = Lab()
    lab.set_up()
    try:
        yield lab
    finally:
        # The workers go back to no failpoint; the stack and Gitea stay up to look at.
        lab.workers_up()
        print(f"\nThe issues: {GITEA_URL}/{ADMIN}/{REPO}/issues", flush=True)
        lab.api.close()
        lab.gitea.close()
