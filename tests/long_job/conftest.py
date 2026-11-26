"""The long-job harness: one generated access review, run as a job on the long stack and graded.

The stack is the compose stack with docker-compose.long.yml on top. `make
long-job` starts it and runs these tests on the fake provider, which plays
the scripted reviewer of `rules.scripted_reviewer` over the job of the
committed public seed. The harness writes that scenario, starts the fake
provider and the workers on it with the fake's trigger and the
`after_summary` failpoint, submits the job, kills the worker that holds it
twice more, and grades the job with `grader.grade`.

LONG_JOB_PROVIDER names the provider, LONG_JOB_MODE the mode and
LONG_JOB_PHASE the phase. The session ends before any stack call when
`rules.refusals` refuses the run, when the provider is `claude` (a keyed run
is not built), when a must-fail run is given a token budget, or when the
mode or the phase is not known. It ends before the first submission unless
every worker calls the run's provider.

Each session writes its scenario, its record and the workers' logs to
`.long-job/<time>/`. What it prints and records are counts and fixed
sentences: no title, id, code or other text of the job. An error of the
harness is kept as its type's name only.
"""

import json
import os
import platform
import re
import subprocess
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx2
import pytest

from access_review import Review, answer_key, generate, read_seed, spec
from runtime.logs import json_safe, text_safe
from runtime.provider.messages import CLAUDE_API_URL

from . import grader, rules

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ("docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.long.yml")
# The provider URL every worker must have, by the run's provider.
URLS = {"fake": "http://fake-provider:8000", rules.CLAUDE: CLAUDE_API_URL}
# The workers and the lease of the long stack, in seconds: the default.
WORKERS = 2
LEASE = 30.0
# Where a session writes its scenario, its record and the workers' logs.
RECORDS = ".long-job"
PHASE = "LONG_JOB_PHASE"
# `control`: the checkout as it is. `edited`: a mechanism is switched off.
PHASES = ("control", "edited")
# Fixed sentences that stop a session. None quotes a value.
KEYED_NOT_BUILT = "A keyed run is not built."
BAD_PHASE = "The phase is not control or edited."
# The first kill: the worker that stores the job's first summary stops there.
FAILPOINT = "after_summary"
# What a worker logs when it stops at a failpoint.
FIRED = "failpoint reached, stopping the worker"
NOT_FINAL = ("queued", "running", "waiting", "needs_approval")
# Seconds between two reads of the job list, of the job's steps, and of the
# steps from the summary a kill follows until that kill is made.
LIST_POLL = 10.0
STEP_POLL = 2.0
KILL_POLL = 0.2
# Seconds a cancelled job gets to reach a final status.
CANCELLED = 120.0
# Seconds the wall clock may drift from the monotonic clock during the run
# before the run is void. Leases end by the wall clock.
CLOCK_STEP_LIMIT = 1.0
PAGE = 1000
# The name of the record's run, and the number of jobs a session runs.
RECORD_TEST = "long-job"
RUNS = 1
PROVIDER_NAMES = {"fake": "fake provider", rules.CLAUDE: "Claude API"}
# The containers of a service, found by the labels compose sets.
SERVICE = ("--filter", "label=com.docker.compose.project=agent-runtime", "--filter")
# The start of a line of `docker logs --timestamps`, to the microsecond.
STAMP = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,6}))?\d*Z ")
# Why a harness kill was not made. Fixed sentences.
NO_HOLDER = "no worker of the stack holds the job"
NOT_KILLED = "`docker kill` failed"
NOT_STARTED = "`docker start` failed"


# What a session may start with


def keyed_problem(env: Mapping[str, str]) -> str | None:
    """Why a run on the Claude API cannot start: none is built. None on any other provider."""
    return KEYED_NOT_BUILT if env.get(rules.PROVIDER, "") == rules.CLAUDE else None


def session_problems(env: Mapping[str, str], root: Path) -> list[str]:
    """Every reason the session stops before its first stack call, as fixed sentences.

    `rules.refusals` is asked on every provider, the fake included, so a
    provider that is neither the fake nor `claude` is refused. On the fake
    it reads no seed and no key.
    """
    found = list(rules.refusals(env, root))
    keyed = keyed_problem(env)
    if keyed is not None:
        found.append(keyed)
    mode = env.get(rules.MODE, "")
    if mode not in rules.MODES:
        found.append(rules.BAD_MODE)
    elif mode == rules.MUST_FAIL and env.get(rules.BUDGET, ""):
        found.append(rules.BUDGET_GIVEN)
    if env.get(PHASE, "") not in PHASES:
        found.append(BAD_PHASE)
    return list(dict.fromkeys(found))


def may_submit(ready: float | None, now: float, line: rules.Line) -> bool:
    """Whether a job may be submitted at monotonic time `now`.

    The session's clock starts at `ready`, the moment the stack was up and
    checked: the build and the start of the stack do not count. Before
    that, no job is submitted.
    """
    if ready is None:
        return False
    return rules.may_start(max(now - ready, 0.0), line)


# What the session submits and writes


def committed_review(root: Path, line: rules.Line) -> Review:
    """The job of the committed public seed in the line's shape. No other seed is read."""
    return generate(read_seed(root / rules.PUBLIC_SEED_FILE), rules.SHAPES[line.name])


def submission(review: Review, line: rules.Line) -> dict[str, Any]:
    """The body of `POST /jobs`: the review for the fake's reviewer, with the shape's limits."""
    return {
        **spec(review, rules.FAKE_MODEL),
        "token_budget": line.budget_cap,
        "timeout_seconds": line.timeout_seconds,
    }


def scenario_files(review: Review) -> dict[str, dict[str, Any]]:
    """The scenario files of a session, by name: the scripted reviewer, with the wait."""
    return {rules.FAKE_MODEL: rules.scripted_reviewer(review, delay_seconds=rules.DELAY)}


def compose_env(environ: Mapping[str, str], *, scenario_dir: str) -> dict[str, str]:
    """The environment of one compose call: `environ` with every setting of the long stack set.

    The run's own `LONG_JOB_*` variables are left out: no container needs
    them, and a seed in them must reach nothing.
    """
    return {
        **{name: value for name, value in environ.items() if not name.startswith("LONG_JOB_")},
        "LONG_SCENARIO_DIR": scenario_dir,
        "SUMMARY_TRIGGER": str(rules.FAKE_TRIGGER),
        "FAILPOINT": FAILPOINT,
        "FAILPOINT_SUMMARY": "",
        "FAKE_TRACKER_FIRST_ISSUE": "",
    }


def markers(review: Review) -> list[str]:
    """What no record, output or saved log of the session may hold.

    The reference code, every decided grant id (made, cleared and
    carried), every notice line and the first line of every file, the
    index among them.
    """
    key = answer_key(review)
    carried = [planted.grant.id for planted in review.planted if planted.kind == "carried"]
    notices = [notice.text for notice in review.notices]
    first = [text.splitlines()[0] for text in review.files.values() if text.strip()]
    found = {key.reference_code, *key.made, *key.cleared, *carried, *notices, *first}
    return sorted(marker for marker in found if marker)


def markers_in(texts: Sequence[str], found: Sequence[str]) -> int:
    """How many (text, marker) pairs there are where the text holds the marker."""
    return sum(marker in text for text in texts for marker in found)


# Kills


@dataclass
class Planned:
    """A harness kill: `offset` steps after the job's summary number `after`."""

    number: int
    after: int
    offset: int
    target: int | None = None
    kill: grader.Kill | None = None


def plan(seed: bytes, line: rules.Line) -> list[Planned]:
    """Kills 2 and 3 of the line, after its two summaries, at the seed's offsets."""
    return [
        Planned(number, after, rules.offset(seed, line.name, number))
        for number, after in zip((2, 3), line.kill_after, strict=True)
    ]


def summary_seqs(steps: Sequence[Mapping[str, Any]]) -> list[int]:
    return [int(step["seq"]) for step in steps if step["kind"] == "summary"]


def kill_target(steps: Sequence[Mapping[str, Any]], after: int, offset: int) -> int | None:
    """The seq a kill aims at, or None while the summary it follows is not stored."""
    seqs = summary_seqs(steps)
    return seqs[after - 1] + offset if len(seqs) >= after else None


def due(planned: Planned, steps: Sequence[Mapping[str, Any]]) -> bool:
    """Whether a kill not made yet has its target among the steps read."""
    return (
        planned.kill is None
        and planned.target is not None
        and bool(steps)
        and int(steps[-1]["seq"]) >= planned.target
    )


# The workers' logs


def stamp(line: str) -> datetime | None:
    """When a line of `docker logs --timestamps` was written, or None for a line without a time."""
    found = STAMP.match(line)
    if found is None:
        return None
    fraction = (found[2] or "0").ljust(6, "0")
    return datetime.fromisoformat(f"{found[1]}.{fraction}+00:00")


def since(text: str, started: datetime) -> str:
    """The lines of a saved log written at or after `started`."""
    return "\n".join(
        line
        for line in text.splitlines()
        if (written := stamp(line)) is not None and written >= started
    )


def message(line: str) -> str | None:
    """The `message` of a JSON log line, with or without Docker's timestamp before it."""
    text = line if line.startswith("{") else line.partition(" ")[2]
    try:
        entry = json.loads(text)
    except ValueError:
        return None
    found = entry.get("message") if isinstance(entry, dict) else None
    return found if isinstance(found, str) else None


def failpoint_lines(text: str) -> int:
    """How often a worker logged that it stopped at a failpoint. Words inside another field do
    not count."""
    return sum(message(line) == FIRED for line in text.splitlines())


# The pass line of the fake run


def pass_line_misses(record: Mapping[str, Any]) -> list[str]:
    """The items of the fake run's pass line that the record misses, as fixed names.

    The verdict is PASS; the failpoint line is there once; kills 2 and 3
    landed in different windows; the job was taken over at least three
    times; nobody was asked a question; the job stored at least six
    summaries.
    """
    counts = record.get("counts") or {}
    kills = counts.get("kills") or []
    landed = [bool(kill.get("landed")) for kill in kills[:2]]
    landed += [False] * (2 - len(landed))
    windows = [kill.get("window") for kill in kills[:2]]
    takeovers = record.get("takeovers")
    summaries = (record.get("floors") or {}).get("summaries")
    checks = (
        ("verdict", record.get("verdict") == grader.PASS),
        ("failpoint", record.get("failpoint_lines") == 1),
        ("kill_2", landed[0]),
        ("kill_3", landed[1]),
        ("kill_windows", all(landed) and windows[0] != windows[1]),
        ("takeovers", isinstance(takeovers, int) and takeovers >= rules.KILLS),
        ("questions", record.get("questions") == 0),
        ("summaries", isinstance(summaries, int) and summaries >= rules.SHORT.floor_summaries),
    )
    return [name for name, held in checks if not held]


# The stack


class Command(Protocol):
    def __call__(self, *args: str, env: dict[str, str] | None = ..., check: bool = ...) -> str: ...


def docker(*args: str, env: dict[str, str] | None = None, check: bool = True) -> str:
    """Run a command in the repository and return its output, standard error included."""
    done = subprocess.run(  # noqa: S603 - fixed program, arguments come from the harness
        args,
        cwd=REPO_ROOT,
        env=os.environ if env is None else env,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    if check and done.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:3])} ... exited {done.returncode}")
    return done.stdout + done.stderr


FAILED = (RuntimeError, OSError, subprocess.TimeoutExpired)


def show(text: str) -> None:
    """Print a line of the session, with its workflow-command markers escaped."""
    print(text_safe(text), flush=True)


class WrongStack(Exception):
    """A worker is not on the run's provider, or not on the long stack's lease."""


class SubmissionRefused(Exception):
    """The API did not accept the job. Only its status code is kept."""


class NotStarted(Exception):
    """The job's wait would end after the session's deadline."""


@dataclass
class Seen:
    """What the harness saw of the session's job. Counts, seqs, classes, and the steps."""

    job: dict[str, Any] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)
    questions: list[dict[str, Any]] = field(default_factory=list)
    kills: list[grader.Kill] = field(default_factory=list)
    failpoint_lines: int = 0
    verify: int | None = None
    crashed: bool = False
    voids: list[str] = field(default_factory=list)
    seconds: float | None = None
    # The type name of an error of the harness, and the status code of a
    # submission the API did not accept.
    harness_error: str | None = None
    submission_status: int | None = None
    not_started: int = 0
    cancelled: bool = False


def first_request(steps: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    found = [step["payload"] for step in steps if step["kind"] == "model_request"]
    return found[0] if found else {}


def commit() -> str:
    """The commit of the checkout, for a record. Never raises: a run's end writes the record."""
    try:
        head = docker("git", "rev-parse", "--short", "HEAD").strip()
        dirty = docker("git", "status", "--porcelain").strip()
    except FAILED:
        return "unknown"
    return f"{head}{' (uncommitted changes)' if dirty else ''}"


class Stack:
    """The long stack as one session sees it: its scenario, its workers, its one job."""

    def __init__(
        self,
        directory: Path,
        *,
        provider: str = "fake",
        mode: str = rules.MUST_FAIL,
        phase: str = "control",
        command: Command | None = None,
        api: httpx2.Client | None = None,
        tracker: httpx2.Client | None = None,
    ) -> None:
        self.directory = directory
        self.scenarios = directory / "scenarios"
        self.provider = provider
        self.mode = mode
        self.phase = phase
        self.command = command or docker
        self.api = api or httpx2.Client(
            base_url=os.environ.get("RUNTIME_API_URL", "http://127.0.0.1:8000"), timeout=10
        )
        self.tracker = tracker or httpx2.Client(
            base_url=os.environ.get("FAKE_TRACKER_URL", "http://127.0.0.1:8002"), timeout=10
        )
        # The worker containers, read after the `up`. A container that is
        # killed and started again keeps its id.
        self.containers: list[str] = []
        self.submitted: list[str] = []
        self.logs: list[str] = []
        self.markers: list[str] = []
        self.record: dict[str, Any] | None = None

    def hang_up(self) -> None:
        for client in (self.api, self.tracker):
            client.close()

    # The stack

    def compose(self, *args: str, check: bool = True) -> str:
        """One compose call on the long stack, with every setting of the harness named."""
        env = compose_env(os.environ, scenario_dir=str(self.scenarios))
        return self.command(*COMPOSE, *args, env=env, check=check)

    def write_scenarios(self, review: Review) -> None:
        """The session's scenario files. The fake provider reads them once, when it starts."""
        self.scenarios.mkdir(parents=True, exist_ok=True)
        for name, scenario in scenario_files(review).items():
            (self.scenarios / f"{name}.json").write_text(
                json.dumps(scenario, indent=2) + "\n", encoding="utf-8"
            )

    def up(self) -> None:
        """The whole stack on the session's scenario, the fake's trigger and the failpoint."""
        self.compose("up", "--detach", "--wait", "--no-build")
        self.containers = list(self.workers())

    def service(self, name: str) -> dict[str, str]:
        """Container id -> state, for every container of the service, running or not."""
        out = self.command("docker", "ps", "--all", "--no-trunc", *SERVICE,
                           f"label=com.docker.compose.service={name}",
                           "--format", "{{.ID}} {{.State}}")  # fmt: skip
        return dict(line.split() for line in out.splitlines() if line.strip())

    def workers(self) -> dict[str, str]:
        return self.service("worker")

    def setting(self, container: str, name: str) -> str:
        """One variable of a running worker. Never its whole environment: it may hold a key."""
        return self.command("docker", "exec", container, "printenv", name, check=False).strip()

    def wrong_workers(self) -> str | None:
        """Why the running workers are not all on the run's provider with the lease, or None."""
        if self.provider not in URLS:
            return "the run names no provider this harness knows"
        running = [c for c, state in self.workers().items() if state == "running"]
        if not running:
            return "no worker is running"
        for container in running:
            for name, wanted in (
                ("ANTHROPIC_BASE_URL", URLS[self.provider]),
                ("LEASE_SECONDS", f"{LEASE:g}"),
            ):
                found = self.setting(container, name)
                if found != wanted:
                    return (
                        f"worker {container[:12]} has {name} {found or 'unset'};"
                        f" this run needs {wanted}"
                    )
        return None

    def tracker_start(self) -> str:
        """The fake tracker's container and when it started. A restart changes it."""
        found = sorted(self.service("fake-tracker"))
        if len(found) != 1:
            return f"{len(found)} tracker containers"
        return self.command(
            "docker", "inspect", "--format", "{{.Id}} {{.State.StartedAt}}", found[0]
        ).strip()

    def unfinished(self) -> int:
        """How many jobs the API lists that are not final."""
        found = 0
        for status in NOT_FINAL:
            listed = self.api.get("/jobs", params={"status": status, "limit": 200})
            found += len(listed.raise_for_status().json()["jobs"])
        return found

    # The job

    def submit(self, body: Mapping[str, Any]) -> str:
        """Submit the job, after the workers are checked again. Nothing is sent to a wrong stack."""
        if self.wrong_workers() is not None:
            raise WrongStack
        answer = self.api.post("/jobs", json=body)
        if answer.status_code != 201:
            raise SubmissionRefused(answer.status_code)
        job_id = str(answer.json()["id"])
        self.submitted.append(job_id)
        return job_id

    def job(self, job_id: str) -> dict[str, Any]:
        """`GET /jobs/{id}`, reduced: no spec, no error text."""
        return rules.reduced(self.api.get(f"/jobs/{job_id}").raise_for_status().json())

    def listed(self, job_id: str) -> dict[str, Any] | None:
        """The job as the job list shows it, reduced, or None when the list does not hold it."""
        before: str | None = None
        while True:
            params: dict[str, Any] = {"limit": 100}
            if before is not None:
                params["before"] = before
            jobs = self.api.get("/jobs", params=params).raise_for_status().json()["jobs"]
            for job in jobs:
                if str(job["id"]) == job_id:
                    return rules.reduced(job)
            if len(jobs) < 100:
                return None
            before = str(jobs[-1]["id"])

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

    def lease(self, job_id: str) -> tuple[str | None, int | None]:
        """The job's lease owner and lease epoch, read together from one view. Nothing else is
        kept."""
        view = self.api.get(f"/jobs/{job_id}").raise_for_status().json()
        owner, epoch = view.get("lease_owner"), view.get("lease_epoch")
        return (
            owner if isinstance(owner, str) else None,
            epoch if isinstance(epoch, int) else None,
        )

    def holder(self, owner: str | None) -> str | None:
        """The worker container whose worker id `owner` is. A worker's id starts with its host name,
        the first 12 characters of its container's id."""
        if not owner:
            return None
        return next((c for c in self.containers if owner.startswith(c[:12])), None)

    def kill(
        self, job_id: str, planned: Planned, steps: Sequence[Mapping[str, Any]]
    ) -> grader.Kill:
        """Kill the worker that holds the job and start it again.

        The kill ends the lease epoch the job's view names with its owner,
        read together: a takeover after the last step read is then the
        epoch killed. `held` says whether the killed container held the
        job's lease; `latency` is the seconds from the last step's
        `created_at` to the return of `docker kill`.
        """
        last = steps[-1]
        owner, epoch = self.lease(job_id)
        target = self.holder(owner)
        if target is None:
            return grader.Kill(planned.target, epoch, held=False, problem=NO_HOLDER)
        held = bool(owner) and str(owner).startswith(target[:12])
        if not self.done("docker", "kill", target):
            return grader.Kill(planned.target, epoch, held=held, problem=NOT_KILLED)
        returned = datetime.now(UTC)
        latency = round((returned - datetime.fromisoformat(last["created_at"])).total_seconds(), 3)
        problem = None if self.done("docker", "start", target) else NOT_STARTED
        return grader.Kill(planned.target, epoch, held=held, latency=latency, problem=problem)

    def done(self, *args: str) -> bool:
        try:
            self.command(*args)
        except FAILED:
            return False
        return True

    def approve(
        self, job_id: str, question: int, steps: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any] | None:
        """Approve the job's open question. Returns its seq and operation id if it was approved."""
        answer = self.api.post(f"/jobs/{job_id}/approve", json={"question": question})
        if answer.status_code != 200:
            return None
        asked = next((step for step in steps if step["seq"] == question), None)
        operation = None if asked is None else asked["payload"].get("operation_id")
        return {"question": question, "operation_id": operation}

    def cancel(self, job_id: str) -> None:
        """Cancel a job that is not final and wait for its end."""
        deadline = time.monotonic() + CANCELLED
        self.api.post(f"/jobs/{job_id}/cancel")
        while time.monotonic() < deadline:
            if self.job(job_id)["status"] not in NOT_FINAL:
                return
            self.api.post(f"/jobs/{job_id}/cancel")
            time.sleep(KILL_POLL)

    def follow(self, job_id: str, line: rules.Line, seen: Seen, plans: list[Planned]) -> None:
        """Wait for the job's final status, making the harness kills and approving questions.

        The job list is read every 10 s, the steps every 2 s, and every
        0.2 s from the summary a kill follows until that kill is made. At
        the job's harness deadline it is cancelled.
        """
        deadline = time.monotonic() + line.job_minutes * 60
        steps: list[dict[str, Any]] = []
        next_list = next_steps = 0.0
        while True:
            now = time.monotonic()
            if now >= next_steps:
                steps += self.steps(job_id, after=steps[-1]["seq"] if steps else 0)
                for planned in plans:
                    if planned.target is None:
                        planned.target = kill_target(steps, planned.after, planned.offset)
                    if due(planned, steps):
                        planned.kill = self.kill(job_id, planned, steps)
                hurried = any(p.kill is None and p.target is not None for p in plans)
                next_steps = now + (KILL_POLL if hurried else STEP_POLL)
            if now >= next_list:
                next_list = now + LIST_POLL
                listed = self.listed(job_id)
                if listed is not None and listed["status"] not in NOT_FINAL:
                    break
                if listed is not None and listed["question"] is not None:
                    steps += self.steps(job_id, after=steps[-1]["seq"] if steps else 0)
                    approved = self.approve(job_id, int(listed["question"]), steps)
                    if approved is not None:
                        seen.questions.append(approved)
            if now > deadline:
                self.cancel(job_id)
                seen.cancelled = True
                break
            time.sleep(KILL_POLL)
        seen.kills = [
            planned.kill or grader.Kill(planned.target, None, held=False) for planned in plans
        ]

    def read_after(self, job_id: str, seen: Seen) -> None:
        """The job, its steps, the tracker's issues for it and `runtime verify`, after its end."""
        if self.job(job_id)["status"] in NOT_FINAL:
            self.cancel(job_id)
        seen.job = self.job(job_id)
        seen.steps = self.steps(job_id)
        listed = self.tracker.get("/issues").raise_for_status().json()
        seen.issues = [
            issue for issue in listed if str(issue["operation_id"]).startswith(f"{job_id}:")
        ]
        seen.verify = verify(job_id)

    def save_logs(self, started: datetime) -> str:
        """Keep every worker's log, with the time of each line. Returns the lines of the run."""
        directory = self.directory / "logs"
        directory.mkdir(parents=True, exist_ok=True)
        lines = []
        for container in self.workers():
            text = self.command("docker", "logs", "--timestamps", container, check=False)
            name = f"worker-{container[:12]}.log"
            (directory / name).write_text(text, encoding="utf-8")
            self.logs.append(name)
            lines.append(since(text, started))
        return "\n".join(lines)

    # The session's one job

    def run(self) -> dict[str, Any]:
        """Run the session's job, grade it, write its record and return it. Never raises.

        Every error of the harness is kept as its type's name, and makes the
        run void.
        """
        line = rules.MODES[self.mode]
        seen = Seen()
        known: grader.Answers | None = None
        job_id: str | None = None
        tracker = None
        started = datetime.now(UTC)
        wall, monotonic = time.time(), time.monotonic()
        submitted = monotonic
        try:
            review = committed_review(REPO_ROOT, line)
            known = grader.answers(review)
            self.markers = markers(review)
            plans = plan(read_seed(REPO_ROOT / rules.PUBLIC_SEED_FILE), line)
            self.write_scenarios(review)
            self.up()
            if self.wrong_workers() is not None:
                raise WrongStack
            tracker = self.tracker_start()
            ready = time.monotonic()
            if not may_submit(ready, ready, line):
                seen.not_started = 1
                raise NotStarted
            submitted = time.monotonic()
            job_id = self.submit(submission(review, line))
            self.follow(job_id, line, seen, plans)
            seen.seconds = round(time.monotonic() - submitted, 1)
        except SubmissionRefused as refused:
            seen.harness_error = type(refused).__name__
            seen.submission_status = refused.args[0] if refused.args else None
        except Exception as error:
            # The type only: the text could quote a record of the job.
            seen.harness_error = type(error).__name__
        if job_id is not None:
            try:
                self.read_after(job_id, seen)
                if tracker is not None and self.tracker_start() != tracker:
                    seen.voids.append("tracker_restart")
            except Exception as error:
                seen.harness_error = seen.harness_error or type(error).__name__
        if self.containers:
            try:
                text = self.save_logs(started)
                seen.failpoint_lines = failpoint_lines(text)
                seen.crashed = job_id is not None and rules.crashed(text, job_id)
            except Exception as error:
                seen.harness_error = seen.harness_error or type(error).__name__
        drift = (time.time() - wall) - (time.monotonic() - monotonic)
        if abs(drift) > CLOCK_STEP_LIMIT:
            seen.voids.append("clock")
        if seen.harness_error is not None:
            seen.voids.append("harness_error")
        self.record = self.write(record_line(self, line, seen, known))
        return self.record

    def write(self, record: dict[str, Any]) -> dict[str, Any]:
        """Append the record to results.jsonl and print it. Returns it as it was written."""
        line = json_safe(json.dumps(record))
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / "results.jsonl").open("a", encoding="utf-8") as file:
            file.write(line + "\n")
        show(f"\nRESULT {line}")
        written: dict[str, Any] = json.loads(line)
        return written


def verify(job_id: str) -> int | None:
    """The exit status of `runtime verify` for the job, or None when it did not run. Its output
    is not kept."""
    try:
        done = subprocess.run(  # noqa: S603 - a fixed command and the job's id
            ["uv", "run", "--frozen", "runtime", "verify", job_id],  # noqa: S607
            cwd=REPO_ROOT,
            capture_output=True,
            check=False,
            timeout=120,
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    return done.returncode


def graded(seen: Seen, known: grader.Answers, line: rules.Line) -> grader.Graded:
    log = grader.Log(
        job=seen.job,
        steps=seen.steps,
        issues=seen.issues,
        questions=seen.questions,
        kills=seen.kills,
        failpoint_lines=seen.failpoint_lines,
        verify=seen.verify,
        count=None,
        voids=tuple(dict.fromkeys(seen.voids)),
        crashed=seen.crashed,
        seconds=seen.seconds,
    )
    return grader.grade(log, known, shape=line, crossing_floor=rules.CROSSING_FLOOR)


def record_line(
    stack: Stack, line: rules.Line, seen: Seen, known: grader.Answers | None
) -> dict[str, Any]:
    """The session's record: its settings, the verdict, every clause, the floors and counts only."""
    voids = list(dict.fromkeys(seen.voids))
    result: grader.Graded | None = None
    if known is not None:
        try:
            result = graded(seen, known, line)
        except Exception as error:
            seen.harness_error = seen.harness_error or type(error).__name__
            voids = list(dict.fromkeys([*voids, "harness_error"]))
    if result is None:
        result = grader.Graded(grader.VOID, {f"void_{cause}": 1 for cause in voids}, {}, {})
    request = first_request(seen.steps)
    return {
        "test": RECORD_TEST,
        "runs": RUNS,
        "mode": stack.mode,
        "phase": stack.phase,
        "shape": line.name,
        "commit": commit(),
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "provider": PROVIDER_NAMES[stack.provider],
        "model": rules.MODEL if stack.provider == rules.CLAUDE else rules.FAKE_MODEL,
        "workers": WORKERS,
        "lease_seconds": LEASE,
        "token_budget": line.budget_cap,
        "machine": f"{platform.system()} {platform.machine()}, {os.cpu_count()} CPUs",
        "where": "GitHub Actions" if os.environ.get("GITHUB_ACTIONS") else "local Docker Compose",
        "seed": "committed",
        "max_tokens": request.get("max_tokens"),
        "summary_trigger": request.get("summary_trigger"),
        "verdict": result.verdict,
        "clauses": result.clauses,
        "floors": result.floors,
        "voids": voids,
        "crashed": seen.crashed,
        "failpoint_lines": seen.failpoint_lines,
        "takeovers": seen.job.get("takeovers"),
        "questions": len(seen.questions),
        "verify": seen.verify,
        "seconds": seen.seconds,
        "not_started": seen.not_started,
        "cancelled_at_deadline": seen.cancelled,
        "harness_error": seen.harness_error,
        "submission_status": seen.submission_status,
        "counts": result.counts,
        "worker_logs": stack.logs,
    }


# The session

# Everything pytest captured or reported in the session, test by test.
OUTPUT: list[str] = []
_STACK = pytest.StashKey[Stack]()


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Keep what each test printed and how it failed, for the test that looks for answers in it."""
    OUTPUT.extend(content for _, content in report.sections)
    if report.longrepr is not None:
        OUTPUT.append(report.longreprtext)


@pytest.fixture(scope="session")
def stack(request: pytest.FixtureRequest) -> Iterator[Stack]:
    found = request.config.stash[_STACK]
    try:
        yield found
    finally:
        found.hang_up()


@pytest.fixture(scope="session")
def record(stack: Stack) -> dict[str, Any]:
    """The record of the session's one job: counts, the verdict and the clauses. Run once."""
    if stack.mode != rules.MUST_FAIL:
        pytest.skip(f"a test of mode {rules.MUST_FAIL}")
    if stack.record is None:
        stack.run()
    assert stack.record is not None
    return stack.record


def pytest_sessionstart(session: pytest.Session) -> None:
    """End the session unless the run may start, before any stack call; then check the workers."""
    problems = session_problems(os.environ, REPO_ROOT)
    if problems:
        pytest.exit(f"Not run: {' '.join(problems)}", returncode=pytest.ExitCode.USAGE_ERROR)
    provider = os.environ.get(rules.PROVIDER, "") or "fake"
    directory = REPO_ROOT / RECORDS / datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")
    stack = Stack(
        directory, provider=provider, mode=os.environ[rules.MODE], phase=os.environ[PHASE]
    )
    try:
        wrong = stack.wrong_workers()
    except FAILED as error:
        stack.hang_up()
        pytest.exit(
            f"Not run: Docker did not answer ({type(error).__name__}).",
            returncode=pytest.ExitCode.USAGE_ERROR,
        )
    if wrong is not None:
        stack.hang_up()
        pytest.exit(
            f"Not run: {wrong}. Start the stack with `make long-job`.",
            returncode=pytest.ExitCode.USAGE_ERROR,
        )
    try:
        left = stack.unfinished()
    except httpx2.HTTPError as error:
        stack.hang_up()
        pytest.exit(
            f"Not run: the API did not list its jobs ({type(error).__name__}).",
            returncode=pytest.ExitCode.USAGE_ERROR,
        )
    if left:
        stack.hang_up()
        pytest.exit(
            f"Not run: {left} jobs of an earlier session are not final. Remove them with"
            " `make clean`, then `make long-job`.",
            returncode=pytest.ExitCode.USAGE_ERROR,
        )
    session.config.stash[_STACK] = stack
