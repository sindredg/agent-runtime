"""The kill runs' view of the stack: workers to kill, and the records to judge a job by.

The stack is the compose stack with docker-compose.kills.yml on top, and
docker-compose.claude.yml too for a run on the Claude API. `make kills` and
`make kills-claude` start it and run these tests.

KILLS_PROVIDER names the provider, `fake` or `claude`. The session ends
before the first test unless every worker's provider URL is that
provider's, so a run never pays by mistake and never records the wrong
provider. Only that one variable is read from a worker: on the Claude API
its environment holds the key.

Each session writes its results and the workers' logs to `.kills/<time>/`.
"""

import json
import os
import platform
import subprocess
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
import pytest

from .rules import API_JOB_LIMIT, TOKEN_BUDGET, Run, Settings, jobs_taken_over, settings

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = json.loads((REPO_ROOT / "examples" / "ci-triage.json").read_text())
WORKERS = 3
# The lease of docker-compose.kills.yml, in seconds.
LEASE = 6.0
# What a worker logs when it stops at a failpoint.
FIRED = "failpoint reached, stopping the worker"
FINAL = ("succeeded", "failed", "cancelled")
# Seconds the wall clock may drift from the monotonic clock during one test
# before the run is void. Leases end by the wall clock.
CLOCK_STEP_LIMIT = 1.0
# Seconds a job may take to reach a final status, kill and takeover included.
JOB_TIMEOUT = {"fake": 90.0, "claude": 300.0}


def docker(*args: str, env: dict[str, str] | None = None, check: bool = True) -> str:
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
        raise RuntimeError(f"{' '.join(args[:8])} ... failed:\n{done.stdout}\n{done.stderr}")
    return done.stdout + done.stderr


def commit() -> str:
    head = docker("git", "rev-parse", "--short", "HEAD").strip()
    dirty = docker("git", "status", "--porcelain").strip()
    return f"{head}{' (uncommitted changes)' if dirty else ''}"


class Stack:
    def __init__(self, chosen: Settings, directory: Path) -> None:
        self.settings = chosen
        self.directory = directory
        self.compose = ["docker", "compose", "-f", "docker-compose.yml",
                        "-f", "docker-compose.kills.yml"]  # fmt: skip
        if chosen.provider == "claude":
            self.compose += ["-f", "docker-compose.claude.yml"]
        self.api = httpx2.Client(
            base_url=os.environ.get("RUNTIME_API_URL", "http://127.0.0.1:8000"), timeout=10
        )
        self.tracker = httpx2.Client(
            base_url=os.environ.get("FAKE_TRACKER_URL", "http://127.0.0.1:8002"), timeout=10
        )
        self.submitted = 0
        self.saved = 0
        # Seconds the kill times are drawn from. Measured by the first test, or given.
        self.window = chosen.window

    # Workers

    def workers(self) -> dict[str, str]:
        """Container id -> state, for every worker container, running or not."""
        out = docker(*self.compose, "ps", "--all", "--format", "{{.ID}} {{.State}}", "worker")
        return dict(line.split() for line in out.splitlines() if line.strip())

    def wrong_provider(self) -> str | None:
        """Why the running workers are not all on the session's provider, or None."""
        running = [c for c, state in self.workers().items() if state == "running"]
        if not running:
            return "no worker is running"
        for container in running:
            url = docker(
                "docker", "exec", container, "printenv", "ANTHROPIC_BASE_URL", check=False
            ).strip()
            if url != self.settings.url:
                return (
                    f"worker {container} calls {url or 'no URL it was given'},"
                    f" and KILLS_PROVIDER={self.settings.provider} means {self.settings.url}"
                )
        return None

    def save_logs(self) -> None:
        """Keep every worker's log. Changing the workers' settings recreates the containers."""
        directory = self.directory / "logs"
        directory.mkdir(parents=True, exist_ok=True)
        self.saved += 1
        for container in self.workers():
            text = docker("docker", "logs", "--timestamps", container, check=False)
            (directory / f"{self.saved:02}-worker-{container[:12]}.log").write_text(
                text, encoding="utf-8"
            )

    def workers_up(self, failpoint: str = "") -> None:
        """Bring all workers up, stopping at `failpoint`. Containers that already match stay."""
        self.save_logs()
        docker(*self.compose, "up", "--detach", "--wait", "--no-deps", "--no-build",
               "--scale", f"worker={WORKERS}", "worker", env={"FAILPOINT": failpoint})  # fmt: skip
        wrong = self.wrong_provider()
        if wrong is not None:
            raise RuntimeError(wrong)

    def start_stopped_workers(self) -> None:
        for container, state in self.workers().items():
            if state != "running":
                docker("docker", "start", container, check=False)

    def holder(self, job_id: str) -> str | None:
        """The container that holds the job's lease, or None."""
        owner = self.job(job_id)["lease_owner"]
        if not owner:
            return None
        # A worker's id is its host name, which is the container id, and its pid.
        return next((c for c in self.workers() if owner.startswith(c[:12])), None)

    def worker_logs(self, since: datetime) -> str:
        return "".join(
            docker("docker", "logs", "--since", since.isoformat(), container, check=False)
            for container in self.workers()
        )

    # Jobs

    def submit(self) -> str:
        """Submit the triage job with the session's model and the fixed budget."""
        if self.settings.provider == "claude" and self.submitted >= API_JOB_LIMIT:
            raise RuntimeError(f"{API_JOB_LIMIT} jobs were submitted to the Claude API: no more")
        self.submitted += 1
        body = {**EXAMPLE, "model": self.settings.model, "token_budget": TOKEN_BUDGET}
        job_id: str = self.api.post("/jobs", json=body).raise_for_status().json()["id"]
        return job_id

    def job(self, job_id: str) -> dict[str, Any]:
        found: dict[str, Any] = self.api.get(f"/jobs/{job_id}").raise_for_status().json()
        return found

    def steps(self, job_id: str) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = (
            self.api.get(f"/jobs/{job_id}/steps").raise_for_status().json()["steps"]
        )
        return found

    def finish(self, job_id: str, *, submitted: float, note: str = "") -> Run:
        """Wait for a final status. A job that asks for approval gets it, and it is counted.

        `submitted` is the monotonic time of the submission.
        """
        approvals: dict[str, int] = {}
        tools: dict[str, int] = {}
        deadline = time.monotonic() + JOB_TIMEOUT[self.settings.provider]
        while True:
            job = self.job(job_id)
            if job["status"] in FINAL:
                seconds = time.monotonic() - submitted
                break
            if job["status"] == "needs_approval":
                asked = [s for s in self.steps(job_id) if s["kind"] == "approval_requested"][-1]
                answer = self.api.post(f"/jobs/{job_id}/approve", json={"question": asked["seq"]})
                if answer.status_code == 200:
                    operation, tool = asked["payload"]["operation_id"], asked["payload"]["name"]
                    approvals[operation] = approvals.get(operation, 0) + 1
                    tools[tool] = tools.get(tool, 0) + 1
            if time.monotonic() > deadline:
                raise AssertionError(f"job {job_id} was not final in time: {job}")
            time.sleep(0.1)
        issues: dict[str, list[int]] = {}
        # The tracker's own list, not the job's step log.
        for issue in self.tracker.get("/issues").raise_for_status().json():
            if issue["operation_id"].startswith(f"{job_id}:"):
                issues.setdefault(issue["operation_id"], []).append(issue["id"])
        return Run(
            job, self.steps(job_id), issues, approvals, tools, seconds, note, self.settings.model
        )


def totals(runs: list[Run]) -> dict[str, Any]:
    """What a set of jobs adds up to, for the record."""
    tools: dict[str, int] = {}
    for run in runs:
        for tool, count in run.approved_tools.items():
            tools[tool] = tools.get(tool, 0) + count
    return {
        "takeovers": sum(run.job["takeovers"] for run in runs),
        "jobs_taken_over": jobs_taken_over(runs),
        "approvals_by_tool": tools,
        "jobs_with_more_than_one_issue_operation": sum(len(run.issues) > 1 for run in runs),
        "tokens_used": sum(run.job["tokens_used"] for run in runs),
        "most_tokens_in_one_job": max((run.job["tokens_used"] for run in runs), default=0),
    }


# The results of the test that is running. They are written when it is over.
_pending: list[dict[str, Any]] = []
_PASSED = pytest.StashKey[bool]()
_STACK = pytest.StashKey[Stack]()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Any:
    report = yield
    if report.when == "call":
        item.stash[_PASSED] = report.passed
    return report


def record(stack: Stack, name: str, result: dict[str, Any]) -> None:
    """Keep a result with what is needed to judge and repeat it.

    It is written when the test is over, with how the test ended in
    `verdict`: `passed`, `failed`, or `void` when the clock was set.
    """
    chosen = stack.settings
    _pending.append(
        {
            "test": name,
            "commit": commit(),
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "workers": WORKERS,
            "lease_seconds": LEASE,
            "provider": chosen.provider_name,
            "model": chosen.model,
            "token_budget": TOKEN_BUDGET,
            "machine": f"{platform.system()} {platform.machine()}, {os.cpu_count()} CPUs",
            "where": "GitHub Actions"
            if os.environ.get("GITHUB_ACTIONS")
            else "local Docker Compose",
            **result,
        }
    )


def _write_results(directory: Path, verdict: str, void: str | None = None) -> None:
    with (directory / "results.jsonl").open("a", encoding="utf-8") as file:
        for entry in _pending:
            entry["verdict"] = verdict
            if void is not None:
                entry["void"] = void
            file.write(json.dumps(entry) + "\n")
            print(f"\nRESULT {json.dumps(entry)}")
    _pending.clear()


@pytest.fixture(scope="session")
def stack(request: pytest.FixtureRequest) -> Iterator[Stack]:
    found = request.config.stash[_STACK]
    found.workers_up()
    print(
        f"\n{found.settings.provider_name}, model {found.settings.model}, {WORKERS} workers,"
        f" lease {LEASE:g} s, seed {found.settings.seed}. Records: {found.directory}"
    )
    try:
        yield found
    finally:
        # The workers go back to no failpoint, and their logs are kept.
        found.workers_up()
        found.save_logs()
        found.api.close()
        found.tracker.close()


@pytest.fixture(autouse=True)
def steady_clock(request: pytest.FixtureRequest, stack: Stack) -> Iterator[None]:
    """Void a run during which the machine's clock was set.

    A lease ends by the database's wall clock. If that clock is set forward
    while a test runs, a live lease is over at once and the job is taken
    over. A run it happens in shows nothing either way. The test's results
    are written here, after that check, each with its verdict.
    """
    _pending.clear()
    wall, monotonic = time.time(), time.monotonic()
    yield
    step = (time.time() - wall) - (time.monotonic() - monotonic)
    if abs(step) > CLOCK_STEP_LIMIT:
        moved = f"the wall clock moved {step:+.1f} s against the monotonic clock"
        _write_results(stack.directory, "void", void=moved)
        pytest.fail(f"void run: {moved} during this test. Leases run on the wall clock.")
    passed = request.node.stash.get(_PASSED, False)
    _write_results(stack.directory, "passed" if passed else "failed")


def pytest_sessionstart(session: pytest.Session) -> None:
    """End the session before any test unless the run may start as asked."""
    try:
        chosen = settings(os.environ)
    except ValueError as error:
        pytest.exit(f"Not run: {error}", returncode=pytest.ExitCode.USAGE_ERROR)
    directory = REPO_ROOT / ".kills" / datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")
    stack = Stack(chosen, directory)
    wrong = stack.wrong_provider()
    if wrong is not None:
        start = "make kills-claude" if chosen.provider == "claude" else "make kills"
        pytest.exit(
            f"Not run: {wrong}. Start the stack with `{start}`.",
            returncode=pytest.ExitCode.USAGE_ERROR,
        )
    directory.mkdir(parents=True)
    session.config.stash[_STACK] = stack
