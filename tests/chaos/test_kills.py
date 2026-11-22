"""G1 and G2: a job survives its worker being killed, and no tool call is repeated unseen.

G1. Every job succeeds, the last request the provider received for it is the
same conversation as in a run nobody interrupted, and the provider is never
asked an earlier turn again.

G2. For every operation id, the tracker holds at most one issue plus the
reruns a person approved. The runner is that person: it approves every job
that asks and counts.

Random kills take their times from a seed. CHAOS_RUNS and CHAOS_SEED set the
number of runs and the seed.
"""

import os
import random
import time
from datetime import UTC, datetime
from typing import Any

import pytest

from runtime.failpoints import NAMES

from .conftest import (
    FAILPOINT_LEASE,
    FINAL,
    FIRED,
    SHORT_LEASE,
    WORKERS,
    WORKLOAD,
    Chaos,
    Run,
    docker,
    failpoint_missed,
    record,
)

RUNS = int(os.environ.get("CHAOS_RUNS", "20"))
SEED = os.environ.get("CHAOS_SEED", "20261113")
# The slow workload takes about 1.6 s when nobody interrupts it. A kill time
# is drawn from a little more than that, so some kills land after the end.
KILL_WINDOW = 2.0
SCENARIO = WORKLOAD


def verdict(run: Run, uninterrupted: list[dict[str, Any]]) -> list[str]:
    """What is wrong with a run, by the pass lines of G1 and G2. Empty when it passes."""
    problems = []
    if run.job["status"] != "succeeded":
        problems.append(f"G1: status is {run.job['status']} ({run.job['error']})")
    elif run.last_conversation != uninterrupted:
        problems.append("G1: the last request differs from an uninterrupted run's")
    # A request that was cut off is sent again, so a turn may repeat. A turn
    # that comes after a later one means the job was started over, not resumed.
    if run.turns != sorted(run.turns):
        problems.append(f"G1: the provider was asked an earlier turn again: {run.turns}")
    duplicates = run.duplicates()
    if duplicates:
        problems.append(f"G2: more issues than one plus approved reruns: {duplicates}")
    if run.job["status"] == "succeeded" and [len(i) for i in run.issues.values()] == []:
        problems.append("G2: the job succeeded and the tracker has no issue for it")
    return problems


def test_jobs_survive_a_kill_at_a_random_time(
    chaos: Chaos, uninterrupted: list[dict[str, Any]]
) -> None:
    chaos.workers_up()
    failures: list[str] = []
    # A run can have several problems; a failed run is counted once.
    failed_runs = 0
    killed_mid_job = takeovers = approvals = 0
    print(f"\n{RUNS} runs, seed {SEED}, {WORKERS} workers, lease {SHORT_LEASE:g} s, {SCENARIO}")
    for index in range(RUNS):
        # Each run has its own generator, so run N can be repeated on its own.
        kill_after = random.Random(f"{SEED}:{index}").uniform(0, KILL_WINDOW)  # noqa: S311
        job_id = chaos.submit(SCENARIO)
        time.sleep(kill_after)
        victim = chaos.holder(job_id)
        note = f"kill at {kill_after:.2f}s"
        if victim is None:
            note += " (no holder: before the claim or after the end)"
        else:
            docker("docker", "kill", victim)
            killed_mid_job += 1
            docker("docker", "start", victim)
        run = chaos.finish(job_id, note=note)
        chaos.start_stopped_workers()
        takeovers += run.job["takeovers"]
        approvals += sum(run.approvals.values())
        problems = verdict(run, uninterrupted)
        print(f"{index:>3} {'FAIL' if problems else 'ok  '} {run.summary()}")
        failed_runs += bool(problems)
        failures += [f"run {index} (seed {SEED}:{index}): {p} :: {run.summary()}" for p in problems]

    record(
        "G1+G2 random kills",
        {
            "runs": RUNS,
            "seed": SEED,
            "scenario": SCENARIO,
            "lease_seconds": SHORT_LEASE,
            "kills_sent_to_the_holder": killed_mid_job,
            "takeovers": takeovers,
            "approvals": approvals,
            "failed_runs": failed_runs,
        },
    )
    assert not failures, "\n".join(failures)
    # A run of kills that never hit a job proves nothing.
    assert killed_mid_job >= RUNS // 2
    assert takeovers >= RUNS // 4


# What the log must show after the takeover, per failpoint, for the call that opens the issue.
AFTER_TAKEOVER = {
    "after_tool_intent": "the check finds nothing: the call is run again, one issue",
    "after_tool_ran": "the check finds the issue: the result is recovered, the tool does not run",
}


@pytest.mark.parametrize("failpoint", NAMES)
def test_a_job_survives_its_worker_stopping_at_the_failpoint(
    chaos: Chaos, uninterrupted: list[dict[str, Any]], failpoint: str
) -> None:
    chaos.workers_up(lease=FAILPOINT_LEASE, failpoint=failpoint, failpoint_tool="create_issue")
    started = datetime.now(UTC)
    try:
        run = chaos.finish(chaos.submit(SCENARIO), note=f"failpoint {failpoint}")
        logs = chaos.worker_logs(started)
    finally:
        chaos.workers_up()
    print(f"\n{run.summary()}")
    # Before the checks: a failed run is recorded too, with its verdict.
    record(
        f"G1+G2 failpoint {failpoint}",
        {
            "runs": 1,
            "scenario": SCENARIO,
            "lease_seconds": FAILPOINT_LEASE,
            "takeovers": run.job["takeovers"],
            "issues": sum(len(ids) for ids in run.issues.values()),
            "approvals": sum(run.approvals.values()),
            "outcome": AFTER_TAKEOVER.get(failpoint, "the job is resumed from the log"),
        },
    )

    # The failpoint was reached, once: the worker that takes over passes it.
    fired = logs.count(FIRED)
    assert fired == 1, failpoint_missed(fired, run.job, logs)
    assert run.job["takeovers"] == 1
    assert verdict(run, uninterrupted) == []
    assert run.approvals == {}
    assert [len(ids) for ids in run.issues.values()] == [1]

    create = [s for s in run.steps if s["payload"].get("name") == "create_issue"]
    results = [
        s
        for s in run.steps
        if s["kind"] == "tool_result" and "created issue" in s["payload"]["content"]
    ]
    assert len(results) == 1
    if failpoint == "after_tool_intent":
        assert [s["lease_epoch"] for s in create] == [1, 2]
        assert "recovered" not in results[0]["payload"]
    elif failpoint == "after_tool_ran":
        assert [s["lease_epoch"] for s in create] == [1]
        assert results[0]["payload"]["recovered"] is True
        assert results[0]["lease_epoch"] == 2


def test_a_call_nobody_can_vouch_for_waits_for_approval_and_then_runs_again(chaos: Chaos) -> None:
    """The third outcome, end to end.

    The worker stops after the tool ran. The tracker is then frozen, so the
    check of the worker that takes over gets no answer. The job waits for a
    person. The runner unfreezes the tracker and approves. The call runs
    again: two issues for one operation id, one of them an approved rerun.
    """
    chaos.workers_up(
        lease=FAILPOINT_LEASE, failpoint="after_tool_ran", failpoint_tool="create_issue"
    )
    started = datetime.now(UTC)
    tracker = docker("docker", "compose", "ps", "--quiet", "fake-tracker").strip()
    frozen = False
    try:
        job_id = chaos.submit(SCENARIO)
        deadline = time.monotonic() + 30
        while not chaos.read(job_id).issues:
            assert time.monotonic() < deadline, "the tool never created the issue"
            time.sleep(0.02)
        docker("docker", "pause", tracker)
        frozen = True
        # A final status too: a job whose first worker lost its lease before the
        # failpoint never waits. The check of the failpoint below says why.
        parked = chaos.wait_for(
            job_id,
            lambda job: job["status"] in ("needs_approval", *FINAL),
            timeout=60,
            what="needs_approval",
        )
        docker("docker", "unpause", tracker)
        frozen = False
        asked = [s for s in chaos.steps(job_id) if s["kind"] == "approval_requested"]
        run = chaos.finish(job_id, note="approved rerun")
        logs = chaos.worker_logs(started)
    finally:
        if frozen:
            docker("docker", "unpause", tracker, check=False)
        chaos.workers_up()
    print(f"\n{run.summary()}")

    record(
        "G2 approved rerun",
        {
            "runs": 1,
            "scenario": SCENARIO,
            "lease_seconds": FAILPOINT_LEASE,
            "issues": sum(len(ids) for ids in run.issues.values()),
            "approvals": sum(run.approvals.values()),
        },
    )

    fired = logs.count(FIRED)
    assert fired == 1, failpoint_missed(fired, run.job, logs)
    assert parked["status"] == "needs_approval", parked
    assert parked["lease_owner"] is None
    assert len(asked) == 1
    assert "could not tell" in asked[0]["payload"]["reason"]
    operation = asked[0]["payload"]["operation_id"]
    assert run.job["status"] == "succeeded"
    assert run.approvals == {operation: 1}
    # Two issues: the first run's, and the rerun a person approved. No more.
    assert [len(ids) for ids in run.issues.values()] == [2]
    assert run.duplicates() == {}
    # A delivered notification is logged whenever the receiver took it, while
    # the job waits (ADR 0029). It is no part of the order checked here.
    logged = [s for s in run.steps if s["kind"] != "notification_sent"]
    kinds = [s["kind"] for s in logged]
    at = kinds.index("approval_requested")
    assert kinds[at:][:4] == [
        "approval_requested",
        "status_changed",
        "approval_given",
        "tool_intent",
    ]
    # The park is in the log with the move it made (APTS AR-002).
    assert logged[at + 1]["payload"] == {
        "from": "running",
        "to": "needs_approval",
        "cause": "approval_requested",
    }
