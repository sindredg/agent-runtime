"""The chaos tests' view of the stack: workers to kill, and three records to read.

The stack is the compose stack with docker-compose.chaos.yml on top: three
workers, a short lease, failpoints and the crash tool. `make chaos` starts it
and runs these tests.

A test never trusts the step log alone. What happened is read from the fake
provider's request log, the fake tracker's list of issues, and the crash
tool's own log file.
"""

import json
import os
import platform
import re
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ["docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.chaos.yml"]
CHAOS_DIR = REPO_ROOT / ".chaos"
WORKERS = 3
# The lease of the chaos stack and of the default stack, in seconds.
SHORT_LEASE, DEFAULT_LEASE = 2.0, 30.0
FINAL = ("succeeded", "failed", "cancelled")
# Seconds the wall clock may drift from the monotonic clock during one test
# before the run is void. A clock that is only being disciplined stays far below.
CLOCK_STEP_LIMIT = 1.0
# The job the kill tests run: the CI triage workload with every answer half a
# second late, so that there is a middle to be stopped in.
WORKLOAD = "ci-triage-slow"
EXAMPLE = json.loads((REPO_ROOT / "examples" / "ci-triage.json").read_text())


def docker(
    *args: str, env: dict[str, str] | None = None, check: bool = True, both: bool = False
) -> str:
    """Run a command and return its standard output, with standard error too if `both`."""
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
        raise RuntimeError(f"{' '.join(args)} failed:\n{done.stdout}\n{done.stderr}")
    return done.stdout + done.stderr if both else done.stdout


def normalized(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A conversation with the issue number taken out.

    Every job gets its own issue, so the number in the tool result differs
    from run to run. Nothing else in the conversation may.
    """
    text = re.sub(r"created issue \d+:", "created issue N:", json.dumps(messages))
    found: list[dict[str, Any]] = json.loads(text)
    return found


@dataclass
class Run:
    """One job and everything the three records say about it."""

    job: dict[str, Any]
    steps: list[dict[str, Any]]
    # The fake provider's log of the requests that carried this job's id.
    requests: list[dict[str, Any]]
    # The fake tracker's issues, by operation id.
    issues: dict[str, list[int]]
    # Approvals this runner gave, by operation id.
    approvals: dict[str, int] = field(default_factory=dict)
    note: str = ""

    @property
    def last_conversation(self) -> list[dict[str, Any]]:
        answered = [r for r in self.requests if r.get("status") == 200]
        return normalized(answered[-1]["body"]["messages"]) if answered else []

    @property
    def turns(self) -> list[int]:
        """The turn each answered request asked for, in the order the provider got them."""
        return [r["turn"] for r in self.requests if r.get("status") == 200]

    def duplicates(self) -> dict[str, list[int]]:
        """Operation ids with more issues than one plus the approved reruns."""
        return {
            operation: ids
            for operation, ids in self.issues.items()
            if len(ids) > 1 + self.approvals.get(operation, 0)
        }

    def summary(self) -> str:
        job = self.job
        issues = ", ".join(f"{op.split(':')[1]}={ids}" for op, ids in self.issues.items()) or "-"
        return (
            f"{job['id']} {job['status']:<10} takeovers={job['takeovers']}"
            f" epoch={job['lease_epoch']}"
            f" steps={len(self.steps)} approvals={sum(self.approvals.values())}"
            f" issues: {issues} {self.note}"
        )


class Chaos:
    def __init__(self) -> None:
        self.api = httpx2.Client(
            base_url=os.environ.get("RUNTIME_API_URL", "http://127.0.0.1:8000"), timeout=10
        )
        self.provider = httpx2.Client(
            base_url=os.environ.get("FAKE_PROVIDER_URL", "http://127.0.0.1:8001"), timeout=10
        )
        self.tracker = httpx2.Client(
            base_url=os.environ.get("FAKE_TRACKER_URL", "http://127.0.0.1:8002"), timeout=10
        )
        self.lease = SHORT_LEASE
        self.failpoint = ""

    # Workers

    def save_logs(self) -> None:
        """Append every worker's log to a file under .chaos/logs, named by container.

        Changing the workers' settings recreates the containers, and their
        logs go with them. A failure has to keep its logs.
        """
        directory = CHAOS_DIR / "logs"
        directory.mkdir(parents=True, exist_ok=True)
        for container in self.workers():
            text = docker("docker", "logs", "--timestamps", container, check=False, both=True)
            (directory / f"worker-{container[:12]}.log").write_text(text, encoding="utf-8")

    def workers_up(
        self, *, lease: float = SHORT_LEASE, failpoint: str = "", failpoint_tool: str = ""
    ) -> None:
        """Bring all workers up with these settings. Containers that already match are kept."""
        self.save_logs()
        docker(
            *COMPOSE,
            "up",
            "--detach",
            "--wait",
            "--no-deps",
            "--no-build",
            "--scale",
            f"worker={WORKERS}",
            "worker",
            env={
                "CHAOS_LEASE_SECONDS": f"{lease:g}",
                "CHAOS_HEARTBEAT_SECONDS": f"{lease / 3 if lease > SHORT_LEASE else 0.5:g}",
                "FAILPOINT": failpoint,
                "FAILPOINT_TOOL": failpoint_tool,
            },
        )
        self.lease, self.failpoint = lease, failpoint

    def workers(self) -> dict[str, str]:
        """Container id -> state, for every worker container, running or not."""
        out = docker(*COMPOSE, "ps", "--all", "--format", "{{.ID}} {{.State}}", "worker")
        return dict(line.split() for line in out.splitlines() if line.strip())

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

    def logs(self, container: str, since: datetime) -> str:
        # The services log to standard error, and `docker logs` passes that on as its own.
        return docker(
            "docker", "logs", "--since", since.isoformat(), container, check=False, both=True
        )

    def worker_logs(self, since: datetime) -> str:
        return "".join(self.logs(container, since) for container in self.workers())

    # The API

    def submit(self, model: str, **changes: Any) -> str:
        response = self.api.post("/jobs", json={**EXAMPLE, "model": model, **changes})
        response.raise_for_status()
        job_id: str = response.json()["id"]
        return job_id

    def job(self, job_id: str) -> dict[str, Any]:
        found: dict[str, Any] = self.api.get(f"/jobs/{job_id}").raise_for_status().json()
        return found

    def steps(self, job_id: str) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = (
            self.api.get(f"/jobs/{job_id}/steps").raise_for_status().json()["steps"]
        )
        return found

    def wait_for(self, job_id: str, reached: Any, *, timeout: float, what: str) -> dict[str, Any]:
        """Poll the job until `reached(job)` is true."""
        deadline = time.monotonic() + timeout
        while True:
            job = self.job(job_id)
            if reached(job):
                return job
            if time.monotonic() > deadline:
                raise AssertionError(f"job {job_id} did not reach {what} in {timeout:g} s: {job}")
            time.sleep(0.05)

    def finish(self, job_id: str, *, timeout: float = 90.0, note: str = "") -> Run:
        """Wait for a final status. A job that asks for approval gets it, and it is counted."""
        approvals: dict[str, int] = {}
        deadline = time.monotonic() + timeout
        while True:
            job = self.job(job_id)
            if job["status"] in FINAL:
                break
            if job["status"] == "needs_approval":
                asked = [s for s in self.steps(job_id) if s["kind"] == "approval_requested"]
                operation = asked[-1]["payload"]["operation_id"]
                if self.api.post(f"/jobs/{job_id}/approve").status_code == 200:
                    approvals[operation] = approvals.get(operation, 0) + 1
            if time.monotonic() > deadline:
                raise AssertionError(f"job {job_id} was not final after {timeout:g} s: {job}")
            time.sleep(0.1)
        return self.read(job_id, approvals=approvals, note=note)

    def read(self, job_id: str, *, approvals: dict[str, int] | None = None, note: str = "") -> Run:
        issues: dict[str, list[int]] = {}
        for issue in self.tracker.get("/issues").raise_for_status().json():
            if issue["operation_id"].startswith(f"{job_id}:"):
                issues.setdefault(issue["operation_id"], []).append(issue["id"])
        requests = (
            self.provider.get("/_fake/requests", params={"user_id": job_id})
            .raise_for_status()
            .json()
        )
        return Run(self.job(job_id), self.steps(job_id), requests, issues, approvals or {}, note)


def commit() -> str:
    head = docker("git", "rev-parse", "--short", "HEAD").strip()
    dirty = docker("git", "status", "--porcelain").strip()
    return f"{head}{' (uncommitted changes)' if dirty else ''}"


def record(name: str, result: dict[str, Any]) -> None:
    """Keep a result with what is needed to judge and repeat it."""
    entry = {
        "test": name,
        "commit": commit(),
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "workers": WORKERS,
        "provider": "fake provider",
        "machine": f"{platform.system()} {platform.machine()}, {os.cpu_count()} CPUs",
        "where": "GitHub Actions" if os.environ.get("GITHUB_ACTIONS") else "local Docker Compose",
        **result,
    }
    CHAOS_DIR.mkdir(exist_ok=True)
    with (CHAOS_DIR / "results.jsonl").open("a", encoding="utf-8") as file:
        file.write(json.dumps(entry) + "\n")
    print(f"\nRESULT {json.dumps(entry)}")


@pytest.fixture(autouse=True)
def steady_clock() -> Iterator[None]:
    """Void a run during which the machine's clock was set.

    A lease ends by the database's wall clock. If that clock is set forward
    while a test runs, for example when a virtual machine resumes, a live
    lease is over at once and the job is taken over. That is a known limit of
    the design, not what these tests are about, and a run it happens in
    proves nothing either way. The wall clock is compared with the monotonic
    clock, which is never set.
    """
    wall, monotonic = time.time(), time.monotonic()
    yield
    step = (time.time() - wall) - (time.monotonic() - monotonic)
    if abs(step) > CLOCK_STEP_LIMIT:
        pytest.fail(
            f"void run: the wall clock moved {step:+.1f} s against the monotonic clock"
            " during this test. Leases run on the wall clock, so the result does not count."
        )


@pytest.fixture(scope="session")
def chaos() -> Iterator[Chaos]:
    stack = Chaos()
    stack.workers_up()
    try:
        yield stack
    finally:
        for client in (stack.api, stack.provider, stack.tracker):
            client.close()


@pytest.fixture(scope="session")
def uninterrupted(chaos: Chaos) -> list[dict[str, Any]]:
    """The last conversation of the workload job when nobody interrupts it.

    The same scenario as the interrupted runs: the provider's ids for tool
    calls carry the scenario's name.
    """
    run = chaos.finish(chaos.submit(WORKLOAD))
    assert run.job["status"] == "succeeded", run.summary()
    assert run.job["takeovers"] == 0
    assert [len(ids) for ids in run.issues.values()] == [1]
    return run.last_conversation
