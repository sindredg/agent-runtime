"""G10: a tool call that needs approval does not run before a person approved it.

Every job asks for approval before `create_issue`, through its spec. Workers
are killed at random times, before the question and after the answer. The
runner is the person: it approves some jobs and denies others.

What ran is read from the fake tracker's own list, which stamps each issue
when it is created. For every call:

- the tracker holds no more issues than the log holds approvals for it, and
  each issue was created after an approval of its own;
- a call that was denied has no issue at all;
- a job nobody answers is `failed` at its deadline, without an issue.

Random kills take their times from a seed. CHAOS_RUNS and CHAOS_SEED set the
number of runs and the seed.
"""

import os
import random
import time
from datetime import UTC, datetime
from typing import Any

import pytest

from .conftest import (
    FAILPOINT_LEASE,
    FINAL,
    FIRED,
    SHORT_LEASE,
    WORKERS,
    WORKLOAD,
    Chaos,
    docker,
    failpoint_missed,
    record,
)

RUNS = int(os.environ.get("CHAOS_RUNS", "20"))
SEED = os.environ.get("CHAOS_SEED", "20261113")
SCENARIO = WORKLOAD
GATED = ["create_issue"]
# The slow workload reaches its question after about 1.2 s, and takes about
# 0.8 s from the answer to its end. A kill time is drawn from a little more
# than each, so kills land before, at and after both.
ASK_WINDOW = 1.5
RUN_WINDOW = 1.0
# The pass line of G7, which the deadline of a job that waits falls under.
WITHIN = 5.0


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


def kill_holder(chaos: Chaos, job_id: str) -> bool:
    """Kill the worker that holds the job, and start it again. False if nobody holds it."""
    victim = chaos.holder(job_id)
    if victim is None:
        return False
    docker("docker", "kill", victim)
    docker("docker", "start", victim)
    return True


class Gated:
    """One job that asks before `create_issue`, and what three records say about it."""

    def __init__(self, chaos: Chaos, job_id: str) -> None:
        self.job = chaos.job(job_id)
        self.steps = chaos.steps(job_id)
        issues = chaos.tracker.get("/issues").raise_for_status().json()
        # The tracker's record: when each issue of this job was created, by call.
        self.runs: dict[str, list[datetime]] = {}
        for issue in issues:
            if issue["operation_id"].startswith(f"{job_id}:"):
                self.runs.setdefault(issue["operation_id"], []).append(at(issue["created_at"]))

    def answered(self, kind: str) -> dict[str, list[datetime]]:
        """When each call got an answer of this kind, by the step log."""
        found: dict[str, list[datetime]] = {}
        for step in self.steps:
            if step["kind"] == kind:
                found.setdefault(step["payload"]["operation_id"], []).append(at(step["created_at"]))
        return found

    def problems(self) -> list[str]:
        """What is wrong by the pass line of G10. Empty when the job passes."""
        problems = []
        approvals, denials = self.answered("approval_given"), self.answered("approval_denied")
        for operation, runs in self.runs.items():
            given = sorted(approvals.get(operation, []))
            if len(runs) > len(given):
                problems.append(
                    f"{len(runs)} runs of {operation} for {len(given)} approvals: a run"
                    " without an approval of its own"
                )
            # The first run after the first approval, the second after the second.
            early = [
                run for run, approval in zip(sorted(runs), given, strict=False) if run <= approval
            ]
            if early:
                problems.append(f"{operation} ran before its approval: {early}")
            if operation in denials and operation not in approvals:
                problems.append(f"{operation} was denied and ran all the same")
        return problems

    def summary(self) -> str:
        count = {
            kind: sum(1 for step in self.steps if step["kind"] == f"approval_{kind}")
            for kind in ("requested", "given", "denied")
        }
        return (
            f"{self.job['id']} {self.job['status']:<10} takeovers={self.job['takeovers']}"
            f" asked={count['requested']} given={count['given']} denied={count['denied']}"
            f" issues={sum(len(runs) for runs in self.runs.values())}"
        )


def run_gated(chaos: Chaos, job_id: str, answer: str, rng: random.Random) -> tuple[Gated, int]:
    """Kill, answer every question with `answer`, kill once more, and wait for the end.

    Returns the job and how many kills hit a worker that held it.
    """
    time.sleep(rng.uniform(0, ASK_WINDOW))
    kills = int(kill_holder(chaos, job_id))
    answers = 0
    deadline = time.monotonic() + 120
    while (job := chaos.job(job_id))["status"] not in FINAL:
        waits = job["status"] == "needs_approval"
        if waits and chaos.answer(job_id, answer) == 200:
            answers += 1
            if answers == 1:
                # Once, after the first answer: in the claim, between the
                # intent and the run, in the run, or after it.
                time.sleep(rng.uniform(0, RUN_WINDOW))
                kills += int(kill_holder(chaos, job_id))
        assert time.monotonic() < deadline, f"job {job_id} was not final after 120 s: {job}"
        time.sleep(0.05)
    chaos.start_stopped_workers()
    return Gated(chaos, job_id), kills


def test_a_gated_call_never_runs_before_its_approval_under_random_kills(chaos: Chaos) -> None:
    chaos.workers_up()
    failures: list[str] = []
    # A run can have several problems; a failed run is counted once.
    failed_runs = 0
    kills = takeovers = approved_jobs = denied_jobs = approvals = denials = issues = 0
    print(f"\n{RUNS} runs, seed {SEED}, {WORKERS} workers, lease {SHORT_LEASE:g} s, {SCENARIO}")
    for index in range(RUNS):
        # Each run has its own generator, so run N can be repeated on its own.
        rng = random.Random(f"{SEED}:approval:{index}")  # noqa: S311
        answer = "deny" if index % 3 == 2 else "approve"
        job_id = chaos.submit(SCENARIO, needs_approval=GATED)
        gated, hit = run_gated(chaos, job_id, answer, rng)
        problems = gated.problems()
        given = sum(len(times) for times in gated.answered("approval_given").values())
        denied = sum(len(times) for times in gated.answered("approval_denied").values())
        ran = sum(len(runs) for runs in gated.runs.values())
        if gated.job["status"] != "succeeded":
            problems.append(f"status is {gated.job['status']} ({gated.job['error']})")
        if answer == "approve" and (ran != 1 or given < 1):
            problems.append(f"an approved job has {ran} issues for {given} approvals, not one")
        if answer == "deny" and (ran != 0 or denied < 1 or given != 0):
            problems.append(f"a denied job has {ran} issues, {denied} denials, {given} approvals")
        kills += hit
        takeovers += gated.job["takeovers"]
        approvals += given
        denials += denied
        issues += ran
        approved_jobs += answer == "approve"
        denied_jobs += answer == "deny"
        print(f"{index:>3} {'FAIL' if problems else 'ok  '} {answer:<7} {gated.summary()}")
        failed_runs += bool(problems)
        failures += [f"run {index} (seed {SEED}:approval:{index}): {p}" for p in problems]

    record(
        "G10 random kills",
        {
            "runs": RUNS,
            "seed": SEED,
            "scenario": SCENARIO,
            "lease_seconds": SHORT_LEASE,
            "kills_sent_to_the_holder": kills,
            "takeovers": takeovers,
            "approved_jobs": approved_jobs,
            "denied_jobs": denied_jobs,
            "approvals": approvals,
            "denials": denials,
            "issues": issues,
            "failed_runs": failed_runs,
        },
    )
    assert not failures, "\n".join(failures)
    # A run of kills that never hit a job proves nothing.
    assert kills >= RUNS // 2
    assert takeovers >= RUNS // 4


# What the log must show when the approved call's worker stops at the failpoint.
AT_FAILPOINT = {
    "after_tool_intent": (2, "the run had not started: a person is asked again, then it runs"),
    "after_tool_ran": (1, "the check finds the issue: its result is stored, nothing runs again"),
}


@pytest.mark.parametrize("failpoint", list(AT_FAILPOINT))
def test_an_approval_covers_one_run_when_the_worker_stops_at_the_failpoint(
    chaos: Chaos, failpoint: str
) -> None:
    approvals_needed, outcome = AT_FAILPOINT[failpoint]
    chaos.workers_up(lease=FAILPOINT_LEASE, failpoint=failpoint, failpoint_tool="create_issue")
    started = datetime.now(UTC)
    try:
        run = chaos.finish(
            chaos.submit(SCENARIO, needs_approval=GATED), note=f"gated, failpoint {failpoint}"
        )
        logs = chaos.worker_logs(started)
    finally:
        chaos.workers_up()
    fired = logs.count(FIRED)
    gated = Gated(chaos, run.job["id"])
    print(f"\n{gated.summary()}")
    record(
        f"G10 failpoint {failpoint}",
        {
            "runs": 1,
            "scenario": SCENARIO,
            "lease_seconds": FAILPOINT_LEASE,
            "takeovers": run.job["takeovers"],
            "approvals": sum(run.approvals.values()),
            "issues": sum(len(runs) for runs in gated.runs.values()),
            "outcome": outcome,
        },
    )

    assert fired == 1, failpoint_missed(fired, run.job, logs)
    assert run.job["status"] == "succeeded"
    assert run.job["takeovers"] == 1
    assert gated.problems() == []
    # One issue, and as many approvals as runs that were started.
    assert [len(runs) for runs in gated.runs.values()] == [1]
    assert sum(run.approvals.values()) == approvals_needed
    kinds = [s["kind"] for s in run.steps]
    assert kinds.count("approval_requested") == approvals_needed


def test_a_gated_job_nobody_answers_is_failed_at_its_deadline(chaos: Chaos) -> None:
    chaos.workers_up()
    rng = random.Random(f"{SEED}:approval:unanswered")  # noqa: S311
    timeout = 8
    job_id = chaos.submit(SCENARIO, needs_approval=GATED, timeout_seconds=timeout)
    time.sleep(rng.uniform(0, ASK_WINDOW))
    killed = kill_holder(chaos, job_id)
    deadline = at(chaos.job(job_id)["deadline"])
    job = chaos.wait_for(
        job_id, lambda j: j["status"] in FINAL, timeout=timeout + 30, what="a final status"
    )
    chaos.start_stopped_workers()
    gated = Gated(chaos, job_id)
    # Both times are the stack's own: the deadline the API set and the
    # database's time for the step that failed the job.
    failed_at = [at(s["created_at"]) for s in gated.steps if s["kind"] == "error"]
    assert len(failed_at) == 1
    late = (failed_at[0] - deadline).total_seconds()
    print(f"\n{gated.summary()} failed {late:.2f} s after the deadline")
    record(
        "G10 nobody answers",
        {
            "runs": 1,
            "seed": SEED,
            "scenario": SCENARIO,
            "lease_seconds": SHORT_LEASE,
            "timeout_seconds": timeout,
            "kills_sent_to_the_holder": int(killed),
            "seconds_after_the_deadline": round(late, 3),
            "issues": sum(len(runs) for runs in gated.runs.values()),
        },
    )

    assert (job["status"], job["error"]) == ("failed", "the job's deadline passed")
    assert 0 <= late <= WITHIN
    kinds: list[Any] = [s["kind"] for s in gated.steps]
    # The job was waiting for its answer, and never got one.
    assert kinds.count("approval_requested") >= 1
    assert kinds.count("approval_given") == kinds.count("approval_denied") == 0
    assert "tool_intent" not in [
        s["kind"] for s in gated.steps if s["payload"].get("name") == "create_issue"
    ]
    assert gated.runs == {}
