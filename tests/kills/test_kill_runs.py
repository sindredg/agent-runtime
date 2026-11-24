"""Kill runs that are judged without a reference run, so they can run on a real model.

A job reads a CI log and opens an issue. Its worker is killed at a random
time, or ends its own process at a failpoint inside the call that opens the
issue. The pass line is in `rules.py`: the job succeeds, the tracker's own
list has an issue for it and no call's issue twice, and the job stayed
inside its token budget.

The tests run in the order of this file. The first one measures how long
the job takes; the random kills need that number.
"""

import time
from datetime import UTC, datetime

import pytest

from runtime.logs import text_safe

from .conftest import FIRED, WORKERS, Stack, docker, record, show, totals
from .rules import (
    FAILPOINTS,
    UNINTERRUPTED,
    Run,
    failpoint_problems,
    kill_time,
    problems,
    too_few_kills,
    uninterrupted_problems,
)


def test_the_job_passes_when_nobody_interrupts_it(stack: Stack) -> None:
    """A job that fails with nobody interrupting it says nothing about kills.

    The longest of these jobs is the window the kill times are drawn from,
    so some kills land after the end of a job that happened to be quick.
    """
    stack.workers_up()
    runs: list[Run] = []
    failures: list[str] = []
    failed_runs = 0
    for index in range(UNINTERRUPTED):
        submitted = time.monotonic()
        run = stack.finish(stack.submit(), submitted=submitted, note="not interrupted")
        runs.append(run)
        wrong = uninterrupted_problems(run)
        show(f"{index:>3} {'FAIL' if wrong else 'ok  '} {run.summary()}")
        failed_runs += bool(wrong)
        failures += [f"job {index}: {p} :: {run.summary()}" for p in wrong]
    longest = max(run.seconds for run in runs)
    record(
        stack,
        "jobs nobody interrupts",
        {
            "runs": UNINTERRUPTED,
            "seconds": [round(run.seconds, 2) for run in runs],
            "failed_runs": failed_runs,
            "problems": len(failures),
            **totals(runs),
        },
    )
    assert not failures, text_safe("\n".join(failures))
    if stack.window is None:
        stack.window = longest


def test_jobs_survive_a_kill_at_a_random_time(stack: Stack) -> None:
    chosen = stack.settings
    assert stack.window is not None, (
        "no kill window: the uninterrupted jobs did not run or did not pass."
        " To repeat a recorded run, give its window as KILLS_WINDOW."
    )
    window = stack.window
    stack.workers_up()
    runs: list[Run] = []
    failures: list[str] = []
    failed_runs = sent = 0
    print(f"\n{chosen.runs} runs, seed {chosen.seed}, window {window:.2f} s")
    for index in range(chosen.runs):
        kill_after = kill_time(chosen.seed, index, window)
        submitted = time.monotonic()
        job_id = stack.submit()
        time.sleep(kill_after)
        victim = stack.holder(job_id)
        note = f"kill at {kill_after:.2f}s"
        if victim is None:
            note += " (no holder: before the claim or after the end)"
        else:
            docker("docker", "kill", victim)
            # Counted only if the victim still held the job after the kill.
            # It may have ended or moved between the read and the kill.
            if stack.holder(job_id) == victim:
                sent += 1
            else:
                note += " (the job left the worker before the kill)"
            docker("docker", "start", victim)
        run = stack.finish(job_id, submitted=submitted, note=note)
        stack.start_stopped_workers()
        runs.append(run)
        wrong = problems(run)
        show(f"{index:>3} {'FAIL' if wrong else 'ok  '} {run.summary()}")
        failed_runs += bool(wrong)
        failures += [
            f"run {index} (seed {chosen.seed}:{index}, window {window:.2f}): {p} :: {run.summary()}"
            for p in wrong
        ]

    counted = totals(runs)
    record(
        stack,
        "random kills",
        {
            "runs": chosen.runs,
            "seed": chosen.seed,
            "window_seconds": round(window, 2),
            "kills_sent_to_the_holder": sent,
            "failed_runs": failed_runs,
            "problems": len(failures),
            **counted,
        },
    )
    assert not failures, text_safe("\n".join(failures))
    # A run of kills that never hit a job proves nothing.
    assert too_few_kills(chosen.runs, sent, counted["jobs_taken_over"]) == []


@pytest.mark.parametrize("failpoint", FAILPOINTS)
def test_jobs_survive_their_worker_stopping_inside_the_call(stack: Stack, failpoint: str) -> None:
    """The worker ends its own process around the tool run, where a random kill rarely lands.

    After the intent is stored and before the tool starts, or after the tool
    ran and before its result is stored. The next worker finds the
    unfinished call in the step log and asks the tracker before it decides.
    """
    chosen = stack.settings
    stack.workers_up(failpoint)
    runs: list[Run] = []
    failures: list[str] = []
    failed_runs = fired_in_all = 0
    try:
        for index in range(chosen.failpoint_runs):
            started, submitted = datetime.now(UTC), time.monotonic()
            run = stack.finish(stack.submit(), submitted=submitted, note=f"failpoint {failpoint}")
            fired = stack.worker_logs(started).count(FIRED)
            fired_in_all += fired
            runs.append(run)
            wrong = failpoint_problems(run, failpoint, fired)
            show(f"{index:>3} {'FAIL' if wrong else 'ok  '} {run.summary()} fired={fired}")
            failed_runs += bool(wrong)
            failures += [f"{failpoint} run {index}: {p} :: {run.summary()}" for p in wrong]
            # The worker that stopped comes back by itself, after a wait that
            # grows each time. The next job should find all of them.
            _wait_for_workers(stack)
    finally:
        stack.workers_up()
    record(
        stack,
        f"failpoint {failpoint}",
        {
            "runs": chosen.failpoint_runs,
            "failpoint_fired": fired_in_all,
            "failed_runs": failed_runs,
            "problems": len(failures),
            **totals(runs),
        },
    )
    assert not failures, text_safe("\n".join(failures))


def _wait_for_workers(stack: Stack, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        states = stack.workers()
        if len(states) == WORKERS and set(states.values()) == {"running"}:
            return
        assert time.monotonic() < deadline, f"not every worker came back in {timeout:g} s: {states}"
        time.sleep(0.5)
