"""The long-job suite's view of the stack: the job, its records, and workers to stop.

The stack is the compose stack with docker-compose.long.yml on top, on the
fake provider. `make long` starts it and runs these tests. The session ends
before the first test unless every worker calls the fake provider with the
default lease, and unless no job of an earlier session is still unfinished.

The suite sets the stack's trigger, scenarios, failpoint and first issue
number itself on every compose call (`rules.compose_env`): none comes from
the shell. Its scenario files are written when the session starts, before
the fake provider is started on them.

Each session writes its scenarios, results and the workers' logs to
`.long/<time>/`.
"""

import json
import os
import platform
import re
import subprocess
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
import pytest

from runtime.logs import json_safe, text_safe

from . import rules
from .rules import Seen

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGED = (
    REPO_ROOT / "services" / "fake_provider" / "src" / "fake_provider" / "scenarios"
) / f"{rules.MODEL}.json"
COMPOSE = ("docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.long.yml")
FAKE_PROVIDER = "http://fake-provider:8000"
# The tracker's first number when the suite names none: the stack file's default.
FIRST_ISSUE = "100000"
# The containers of a service, found by the labels compose sets. `docker ps`
# needs none of the variables a compose command needs.
SERVICE = ("--filter", "label=com.docker.compose.project=agent-runtime", "--filter")
# Seconds between two looks at a job.
POLL = 0.2
# Seconds a job gets to reach a final status: the whole job, each takeover
# on top, a short job, and a job that was cancelled.
FULL_JOB = 300.0
PER_TAKEOVER = 45.0
SHORT_JOB = 120.0
CANCELLED = 120.0
# Seconds the wall clock may drift from the monotonic clock during one run
# before the run is void. Leases end by the wall clock.
CLOCK_STEP_LIMIT = 1.0
PAGE = 1000
# The start of a line of `docker logs --timestamps`, to the microsecond.
STAMP = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,6}))?\d*Z ")


def docker(*args: str, env: dict[str, str] | None = None, check: bool = True) -> str:
    """Run a command in the repository and return its output, standard error included."""
    done = subprocess.run(  # noqa: S603 - fixed program, arguments come from the tests
        args,
        cwd=REPO_ROOT,
        env=os.environ if env is None else env,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    if check and done.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:8])} ... failed:\n{done.stdout}\n{done.stderr}")
    return done.stdout + done.stderr


FAILED = (RuntimeError, OSError, subprocess.TimeoutExpired)


def done(*args: str) -> bool:
    """Run a command and say whether it succeeded. One that timed out did not."""
    try:
        docker(*args)
    except FAILED:
        return False
    return True


def commit() -> str:
    """The commit of the checkout, for a record. Never raises: a run's end writes records."""
    try:
        head = docker("git", "rev-parse", "--short", "HEAD").strip()
        dirty = docker("git", "status", "--porcelain").strip()
    except FAILED:
        return "unknown"
    return f"{head}{' (uncommitted changes)' if dirty else ''}"


def show(text: str) -> None:
    """Print a line of the run, with its workflow-command markers escaped."""
    print(text_safe(text), flush=True)


def failure(text: str, counts: object) -> str:
    """One line of a failed test's message: the check's fixed text, then what was found.

    Its workflow-command markers are escaped: pytest prints the lines.
    """
    return text_safe(f"{text}: {counts}")


def stamp(line: str) -> datetime | None:
    """When a line of `docker logs --timestamps` was written, or None for a line without a time."""
    found = STAMP.match(line)
    if found is None:
        return None
    fraction = (found[2] or "0").ljust(6, "0")
    return datetime.fromisoformat(f"{found[1]}.{fraction}+00:00")


@dataclass
class Running:
    """One run of the suite: what its record and its checks of the clock need."""

    name: str
    started: datetime
    wall: float
    monotonic: float
    # Seconds the clock had drifted before the run's own measurement began.
    drift_before: float = 0.0
    # What the record holds besides the run's settings. The test fills it in.
    counts: dict[str, Any] = field(default_factory=dict)
    # The names of the worker log files saved during the run.
    logs: list[str] = field(default_factory=list)


class Stack:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.scenarios = directory / "scenarios"
        self.packaged: dict[str, Any] = json.loads(PACKAGED.read_text(encoding="utf-8"))
        # The context window and the number of turns of the job's scenario.
        self.window = int(self.packaged["context_window"])
        self.turns = len(self.packaged["turns"])
        self.api = httpx2.Client(
            base_url=os.environ.get("RUNTIME_API_URL", "http://127.0.0.1:8000"), timeout=10
        )
        self.provider = httpx2.Client(
            base_url=os.environ.get("FAKE_PROVIDER_URL", "http://127.0.0.1:8001"), timeout=30
        )
        self.tracker = httpx2.Client(
            base_url=os.environ.get("FAKE_TRACKER_URL", "http://127.0.0.1:8002"), timeout=10
        )
        # The worker containers, read once after each `up`. A container that
        # is killed and started again keeps its id.
        self.containers: list[str] = []
        # Every job submitted since the run began, so that none is left running.
        self.submitted: list[str] = []
        self.run: Running | None = None
        self.saved = 0

    def hang_up(self) -> None:
        """Close the connections to the API, the fake provider and the tracker."""
        for client in (self.api, self.provider, self.tracker):
            client.close()

    # The stack

    def write_scenarios(self) -> None:
        """The session's scenario files. The fake provider reads them once, when it starts."""
        self.scenarios.mkdir(parents=True)
        for name, scenario in rules.scenario_files(self.packaged).items():
            (self.scenarios / f"{name}.json").write_text(
                json.dumps(scenario, indent=2) + "\n", encoding="utf-8"
            )

    def compose(self, *args: str, check: bool = True, **settings: Any) -> str:
        """One compose call on the long stack, with every setting of the suite named."""
        env = rules.compose_env(os.environ, scenario_dir=str(self.scenarios), **settings)
        return docker(*COMPOSE, *args, env=env, check=check)

    def up(self) -> None:
        """The whole stack on the session's scenarios, with the trigger and no failpoint."""
        self.compose("up", "--detach", "--wait", "--no-build", trigger=rules.TRIGGER)
        self.containers = list(self.workers())

    def workers_up(
        self,
        *,
        trigger: int | None = rules.TRIGGER,
        failpoint: str = "",
        failpoint_summary: bool = False,
    ) -> None:
        """Bring both workers up with these settings. Containers that already match stay.

        Their logs are saved first: a container that is made again loses its log.
        """
        self.save_logs()
        self.compose("up", "--detach", "--wait", "--no-deps", "--no-build",
                     "--scale", f"worker={rules.WORKERS}", "worker",
                     trigger=trigger, failpoint=failpoint,
                     failpoint_summary=failpoint_summary)  # fmt: skip
        self.containers = list(self.workers())
        wrong = self.wrong_workers()
        if wrong is not None:
            raise RuntimeError(wrong)

    def tracker_up(self, first_issue: str = "") -> None:
        """Make the fake tracker again, alone, numbering from `first_issue`. Empty: the default."""
        self.compose("up", "--detach", "--wait", "--no-deps", "--no-build", "fake-tracker",
                     trigger=rules.TRIGGER, first_issue=first_issue)  # fmt: skip

    def first_issue(self) -> str:
        """The first number the running tracker was started with."""
        (container,) = self.service("fake-tracker")
        return docker(
            "docker", "exec", container, "printenv", "FAKE_TRACKER_FIRST_ISSUE", check=False
        ).strip()

    def service(self, name: str) -> dict[str, str]:
        """Container id -> state, for every container of the service, running or not."""
        out = docker("docker", "ps", "--all", "--no-trunc", *SERVICE,
                     f"label=com.docker.compose.service={name}",
                     "--format", "{{.ID}} {{.State}}")  # fmt: skip
        return dict(line.split() for line in out.splitlines() if line.strip())

    def workers(self) -> dict[str, str]:
        return self.service("worker")

    def wrong_workers(self) -> str | None:
        """Why the running workers are not all on the fake provider with the lease, or None."""
        running = [c for c, state in self.workers().items() if state == "running"]
        if not running:
            return "no worker is running"
        for container in running:
            for name, wanted in (
                ("ANTHROPIC_BASE_URL", FAKE_PROVIDER),
                ("LEASE_SECONDS", f"{rules.LEASE:g}"),
            ):
                found = docker("docker", "exec", container, "printenv", name, check=False).strip()
                if found != wanted:
                    return (
                        f"worker {container[:12]} has {name} {found or 'unset'};"
                        f" this suite runs with {wanted}"
                    )
        return None

    def setting(self, container: str, name: str) -> str:
        """One variable of a running worker. Never its whole environment."""
        return docker("docker", "exec", container, "printenv", name, check=False).strip()

    def unfinished(self) -> list[str]:
        """The jobs the API lists that are not final, as `id status`."""
        found = []
        for status in rules.NOT_FINAL:
            listed = self.api.get("/jobs", params={"status": status, "limit": 200})
            found += [f"{job['id']} {status}" for job in listed.raise_for_status().json()["jobs"]]
        return found

    def save_logs(self) -> None:
        """Keep every worker's log, with the time of each line."""
        directory = self.directory / "logs"
        directory.mkdir(parents=True, exist_ok=True)
        run = self.run.name if self.run is not None else "session"
        self.saved += 1
        for container in self.workers():
            text = docker("docker", "logs", "--timestamps", container, check=False)
            name = f"{run}-{self.saved:02}-worker-{container[:12]}.log"
            (directory / name).write_text(text, encoding="utf-8")
            if self.run is not None:
                self.run.logs.append(name)

    def worker_log(self, run: Running) -> str:
        """What the workers logged from the run's start, over the files saved in the run.

        A container's log is whole in the last file saved for it, so that
        one counts. A container made again during the run has a file of its own.
        """
        last: dict[str, str] = {}
        for name in run.logs:
            last[name.rsplit("-", 1)[1]] = name
        lines = []
        for name in last.values():
            text = (self.directory / "logs" / name).read_text(encoding="utf-8")
            for line in text.splitlines():
                written = stamp(line)
                if written is not None and written >= run.started:
                    lines.append(line)
        return "\n".join(lines)

    def holder(self, job_id: str) -> str | None:
        """The worker container that holds the job's lease, or None.

        A worker's id starts with its host name, the first 12 characters of
        its container's id. The containers are those of the last `up`.
        """
        owner = self.job(job_id)["lease_owner"]
        if not owner:
            return None
        return next((c for c in self.containers if owner.startswith(c[:12])), None)

    def kill_holder(self, job_id: str, step: dict[str, Any]) -> dict[str, Any]:
        """Kill the worker that holds the job while `step` is its last step, and start it again.

        Returns the step's seq and epoch, the container, and as `latency`
        the seconds from the step's `created_at` to the return of `docker
        kill`. A kill that was not made has no latency and says why in
        `problem`; so does a worker that did not start again. No compose
        call here: the kill has to land while the request is open.
        """
        hit = self._kill(job_id, step)
        if "container" in hit and not done("docker", "start", hit["container"]):
            hit["problem"] = f"`docker start` of {hit['container']} failed"
        return hit

    def _kill(self, job_id: str, step: dict[str, Any]) -> dict[str, Any]:
        """Kill the job's holder, found among the containers of the last `up`."""
        hit: dict[str, Any] = {"seq": step["seq"], "epoch": step["lease_epoch"]}
        holder = self.holder(job_id)
        if holder is None:
            return {**hit, "problem": "no worker of the last `up` holds the job"}
        hit["container"] = holder[:12]
        killed = done("docker", "kill", holder)
        returned = datetime.now(UTC)
        if not killed:
            return {**hit, "problem": f"`docker kill` of {holder[:12]} failed"}
        created = datetime.fromisoformat(step["created_at"])
        return {**hit, "latency": round((returned - created).total_seconds(), 3)}

    def take_over_with(
        self, job_id: str, step: dict[str, Any], *, trigger: int | None
    ) -> dict[str, Any]:
        """Stop every worker dead, let the job's lease run out, and start workers on `trigger`.

        The job's holder is killed first, as `kill_holder` does and with
        the same record: `step` is the job's last step, and the kill has to
        come before the worker's next answer. Then the other workers.
        Killed, not stopped: a worker that is told to stop gives its job
        back, and the next claim is then no takeover. `triggers` is what
        each new worker says it has.
        """
        hit = self._kill(job_id, step)
        killed = hit["container"] if "latency" in hit else None
        for container in self.workers():
            if killed is None or not container.startswith(killed):
                docker("docker", "kill", container, check=False)
        left = [c[:12] for c, state in self.workers().items() if state == "running"]
        if left:
            raise RuntimeError(f"workers still running after the kill: {left}")
        expires = self.job(job_id)["lease_expires_at"]
        if expires is not None:
            wait = (datetime.fromisoformat(expires) - datetime.now(UTC)).total_seconds()
            time.sleep(max(wait, 0) + 1)
        self.workers_up(trigger=trigger)
        running = [c for c, state in self.workers().items() if state == "running"]
        hit["triggers"] = [self.setting(c, "SUMMARY_TRIGGER") for c in running]
        return hit

    # Jobs, and the fake provider's and the tracker's own records

    def clear(self) -> None:
        """Empty the fake provider's log and the tracker's list. Issue numbers go on."""
        self.provider.delete("/_fake/requests").raise_for_status()
        self.tracker.delete("/issues").raise_for_status()

    def submit(self, model: str = rules.MODEL) -> str:
        """Submit the job: the scenario's files as the spec's, no `system`, the default budget."""
        body = {"prompt": rules.PROMPT, "model": model, "files": rules.job_files(self.packaged)}
        job_id: str = self.api.post("/jobs", json=body).raise_for_status().json()["id"]
        self.submitted.append(job_id)
        return job_id

    def job(self, job_id: str) -> dict[str, Any]:
        found: dict[str, Any] = self.api.get(f"/jobs/{job_id}").raise_for_status().json()
        return found

    def steps(self, job_id: str, after: int = 0) -> list[dict[str, Any]]:
        """The job's steps after `after`, read page by page."""
        found: list[dict[str, Any]] = []
        while True:
            page = self.api.get(f"/jobs/{job_id}/steps", params={"after": after, "limit": PAGE})
            steps = page.raise_for_status().json()["steps"]
            found += steps
            if len(steps) < PAGE:
                return found
            after = steps[-1]["seq"]

    def entries(self, job_id: str) -> list[dict[str, Any]]:
        """The fake provider's entries that carry the job's id, in its order."""
        found: list[dict[str, Any]] = (
            self.provider.get("/_fake/requests", params={"user_id": job_id})
            .raise_for_status()
            .json()
        )
        return found

    def log_length(self) -> int:
        return len(self.provider.get("/_fake/requests").raise_for_status().json())

    def seen(self, job_id: str, **more: Any) -> Seen:
        """Everything the checks read about the job, as it is now."""
        issues = rules.issues_of(job_id, self.tracker.get("/issues").raise_for_status().json())
        return Seen(
            job=self.job(job_id),
            steps=self.steps(job_id),
            entries=self.entries(job_id),
            issues=issues,
            log_length=self.log_length(),
            **more,
        )

    def finish(
        self,
        job_id: str,
        *,
        within: float,
        started: datetime,
        submitted: float,
        each: Callable[[list[dict[str, Any]]], None] | None = None,
    ) -> Seen:
        """Wait for a final status, at most `within` seconds, and read the job.

        A job that asks a person gets an approval, so that it goes on, and
        the question is kept: the checks count it. A wait that runs out is
        not an error here. The job comes back as it is, not final.

        `each` gets the job's steps so far at every look, five times a
        second. A run stops a worker from it, at the step it waits for.
        """
        questions: list[dict[str, Any]] = []
        wall, monotonic = time.time(), time.monotonic()
        deadline = submitted + within
        steps: list[dict[str, Any]] = []
        while True:
            if each is not None:
                steps += self.steps(job_id, after=steps[-1]["seq"] if steps else 0)
                each(steps)
            job = self.job(job_id)
            if job["status"] not in rules.NOT_FINAL or time.monotonic() > deadline:
                break
            if job["status"] == "needs_approval":
                asked = rules.of_kind(self.steps(job_id), "approval_requested")[-1]
                answer = self.api.post(f"/jobs/{job_id}/approve", json={"question": asked["seq"]})
                if answer.status_code == 200:
                    payload = asked["payload"]
                    questions.append(
                        {
                            "question": asked["seq"],
                            "operation_id": payload.get("operation_id"),
                            "tool": payload.get("name"),
                            "reason": payload.get("reason"),
                        }
                    )
            time.sleep(POLL)
        seconds = time.monotonic() - submitted
        return self.seen(
            job_id,
            questions=questions,
            started=started,
            seconds=seconds,
            drift=(time.time() - wall) - (time.monotonic() - monotonic),
        )

    def run_job(
        self,
        model: str = rules.MODEL,
        *,
        within: float = FULL_JOB,
        each: Callable[[str, list[dict[str, Any]]], None] | None = None,
    ) -> Seen:
        """Submit the job and wait for its end. `each` gets the job's id and its steps so far."""
        started, submitted = datetime.now(UTC), time.monotonic()
        job_id = self.submit(model)
        watch = None if each is None else (lambda steps: each(job_id, steps))
        return self.finish(job_id, within=within, started=started, submitted=submitted, each=watch)

    def cancel(self, job_id: str) -> dict[str, Any]:
        """Cancel a job that is not final and wait for its end. Returns the job as it is then."""
        deadline = time.monotonic() + CANCELLED
        self.api.post(f"/jobs/{job_id}/cancel")
        while True:
            job = self.job(job_id)
            if job["status"] not in rules.NOT_FINAL or time.monotonic() > deadline:
                return job
            if job["status"] == "needs_approval":
                # A job parked on a question is cancelled by the same call.
                self.api.post(f"/jobs/{job_id}/cancel")
            time.sleep(POLL)

    # A run and its record

    def begin(self, name: str, *, of: Seen | None = None) -> Running:
        """Start a run: its clock, its name for the saved logs, and empty records.

        With `of`, the run is that job, which is already over: the run
        starts when the job did, and nothing is cleared.
        """
        run = Running(name, datetime.now(UTC), time.time(), time.monotonic())
        if of is not None and of.started is not None:
            run.started, run.drift_before = of.started, of.drift
        else:
            self.clear()
        self.run = run
        self.submitted = []
        return run

    def settle(self, run: Running, failures: list[str], job_ids: list[str] | None = None) -> None:
        """After a run, passed or failed: the workers back, no job left running, the logs saved.

        Called from the test's own `finally`, before its one assert. What
        goes wrong here is a line of `failures`, never an exception: the
        workers that did not come back, and a job that is still not final.
        """
        try:
            # First: a run that stopped every worker would leave a job
            # nobody can cancel.
            self.workers_up()
        except FAILED as error:
            failures.append(failure("the workers did not come back after the run", error))
        for job_id in self.submitted if job_ids is None else job_ids:
            try:
                job = self.job(job_id)
                if job["status"] in rules.NOT_FINAL:
                    job = self.cancel(job_id)
                if job["status"] in rules.NOT_FINAL:
                    status = job["status"]
                    failures.append(
                        failure("a job was still not final after its cancel", f"{job_id} {status}")
                    )
            except httpx2.HTTPError as error:
                failures.append(failure("a job could not be read or cancelled", error))
        try:
            self.save_logs()
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
            failures.append(failure("the workers' logs were not saved", error))

    def close(self, run: Running, failures: list[str]) -> None:
        """The last thing of a run, after its last clause: the clock, then the record.

        A run during which the clock was set shows nothing either way. It
        gets a line that says so, and its record the verdict `void`.
        """
        drift = (time.time() - run.wall) - (time.monotonic() - run.monotonic)
        worst = max(drift, run.drift_before, key=abs)
        void = None
        if abs(worst) > CLOCK_STEP_LIMIT:
            void = f"the wall clock moved {worst:+.1f} s against the monotonic clock"
            failures.append(
                failure("void run", f"{void} during this run. Leases run on the wall clock.")
            )
        verdict = "void" if void else ("failed" if failures else "passed")
        self.write(run, verdict, void)

    def write(self, run: Running, verdict: str, void: str | None = None) -> None:
        """One line of results.jsonl for the run, printed too. Counts only, never stored text."""
        entry = {
            "test": run.name,
            "commit": commit(),
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "workers": rules.WORKERS,
            "lease_seconds": rules.LEASE,
            "provider": "fake provider",
            "machine": f"{platform.system()} {platform.machine()}, {os.cpu_count()} CPUs",
            "where": "GitHub Actions"
            if os.environ.get("GITHUB_ACTIONS")
            else "local Docker Compose",
            "seed": None,
            **run.counts,
            "worker_logs": run.logs,
            "verdict": verdict,
        }
        if void is not None:
            entry["void"] = void
        line = json_safe(json.dumps(entry))
        with (self.directory / "results.jsonl").open("a", encoding="utf-8") as file:
            file.write(line + "\n")
        print(f"\nRESULT {line}", flush=True)


_STACK = pytest.StashKey[Stack]()


@pytest.fixture(scope="session")
def stack(request: pytest.FixtureRequest) -> Iterator[Stack]:
    found = request.config.stash[_STACK]
    # The fake provider starts on the session's scenarios here, and the
    # workers get the trigger.
    try:
        found.up()
    except BaseException:
        found.hang_up()
        raise
    wrong = found.wrong_workers()
    if wrong is not None:
        found.hang_up()
        pytest.exit(f"Not run: {wrong}. Start the stack with `make long`.")
    show(
        f"\nfake provider, scenario {rules.MODEL}, {rules.WORKERS} workers,"
        f" lease {rules.LEASE:g} s, trigger {rules.TRIGGER}. Records: {found.directory}"
    )
    try:
        yield found
    finally:
        # Nothing here raises: a failed teardown would turn a failed test into an error.
        try:
            found.run = None
            found.workers_up()
            found.save_logs()
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
            show(f"The stack was not left as it was found: {error}")
        found.hang_up()


@pytest.fixture(scope="session")
def baseline(stack: Stack) -> Seen:
    """The job with nobody interrupting it, run once, when a test first asks for it.

    The other runs are compared with what this job sent. Nothing is checked
    here: the test of the uninterrupted job does that, and has to have
    something to fail on.
    """
    stack.workers_up()
    if stack.first_issue() != FIRST_ISSUE:
        stack.tracker_up()
    stack.clear()
    job = stack.run_job()
    if not job.final:
        # Not left to the test that asks first: under a run of one test that
        # may be any of them. The job comes back as it was read, not final.
        try:
            stack.cancel(job.id)
        except httpx2.HTTPError as error:
            show(f"The uninterrupted job was not final and could not be cancelled: {error}")
    return job


def pytest_sessionstart(session: pytest.Session) -> None:
    """End the session before any test unless the stack is the fake provider's and idle."""
    directory = REPO_ROOT / ".long" / datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")
    stack = Stack(directory)
    wrong = stack.wrong_workers()
    if wrong is not None:
        stack.hang_up()
        pytest.exit(
            f"Not run: {wrong}. Start the stack with `make long`.",
            returncode=pytest.ExitCode.USAGE_ERROR,
        )
    try:
        left = stack.unfinished()
    except httpx2.HTTPError as error:
        stack.hang_up()
        pytest.exit(
            f"Not run: the API did not list its jobs ({error}). Start the stack with `make long`.",
            returncode=pytest.ExitCode.USAGE_ERROR,
        )
    if left:
        # `make long` keeps the database. Such a job would take a worker,
        # and could reach a failpoint meant for another.
        stack.hang_up()
        pytest.exit(
            f"Not run: jobs of an earlier session are not final: {len(left)}, the first"
            f" {left[0]}. Remove them with `make clean`, then `make long`.",
            returncode=pytest.ExitCode.USAGE_ERROR,
        )
    stack.write_scenarios()
    session.config.stash[_STACK] = stack
