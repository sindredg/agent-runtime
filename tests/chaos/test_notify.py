"""G12: a job that waits for a person tells the operator, at least once.

Three runs of jobs that ask before `create_issue` (ADR 0029). Each is judged
by the fake receiver's own log, not by the runtime's steps. A question's id
is the job plus the `seq` of its `approval_requested` step, as in the POST.

- No kill: every question arrives exactly once.
- Workers killed at random times while the receiver holds each POST: every
  question arrives at least once; repeats are counted.
- The receiver down for 30 s: no job stops or fails for it, and every
  question arrives within 60 s of the receiver's return.

A question that is answered before it was sent is closed without a POST. So
each run answers a question only after it arrived, and the first questions
wait past the 12 s a claim holds a row before they are answered: a row that
was sent and not closed would go out again inside that wait.

CHAOS_NOTIFY_JOBS and CHAOS_SEED set the jobs per run and the seed.
"""

import os
import random
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx2

from runtime.logs import text_safe

from .conftest import FINAL, SHORT_LEASE, WORKERS, WORKLOAD, Chaos, docker, record

JOBS = int(os.environ.get("CHAOS_NOTIFY_JOBS", "20"))
SEED = os.environ.get("CHAOS_SEED", "20261113")
SCENARIO = WORKLOAD
GATED = ["create_issue"]
# Seconds every job waits after the last first arrival before it is answered:
# past the 12 s a claim pushes a row's next try, plus a 1 s poll and margin.
HOLD = 16.0
# Seconds the receiver holds each POST in the kill run, so that a kill can
# land between a worker's claim and its record of the answer.
HANG = 1.5
# A kill every this many seconds, drawn from the seed, in the kill run.
KILL_EVERY = (0.5, 2.5)
# The receiver's time down, and the pass line after its return (G12).
DOWN = 30.0
AFTER_RETURN = 60.0
# Seconds a run may take to park every job and post every question.
PARK_WITHIN = 90.0
# A job's own deadline: far past the receiver's time down and its return.
TIMEOUT_SECONDS = 300


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


def arrivals(chaos: Chaos, jobs: list[str]) -> dict[str, list[datetime]]:
    """When each question of these jobs reached the receiver, by its id, oldest first."""
    found: dict[str, list[datetime]] = {}
    for arrival in chaos.receiver.get("/notifications").raise_for_status().json():
        body = arrival["body"]
        if isinstance(body, dict) and body.get("job_id") in jobs:
            found.setdefault(str(body["id"]), []).append(at(arrival["received_at"]))
    return found


def wait_for_the_receiver(chaos: Chaos, timeout: float = 30.0) -> None:
    """Wait until a started receiver answers. Its first seconds refuse or reset connections."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            if chaos.receiver.get("/healthz").status_code == 200:
                return
        except httpx2.TransportError:
            pass
        assert time.monotonic() < deadline, f"the receiver did not answer in {timeout:g} s"
        time.sleep(0.2)


def questions(chaos: Chaos, jobs: list[str]) -> dict[str, dict[str, Any]]:
    """Every question these jobs asked, by its id, from their step logs."""
    return {
        f"{job_id}:{step['seq']}": step
        for job_id in jobs
        for step in chaos.steps(job_id)
        if step["kind"] == "approval_requested"
    }


def submit(chaos: Chaos, count: int) -> list[str]:
    return [
        chaos.submit(SCENARIO, needs_approval=GATED, timeout_seconds=TIMEOUT_SECONDS)
        for _ in range(count)
    ]


def parked(chaos: Chaos, jobs: list[str]) -> list[str]:
    """The jobs that wait on a question now."""
    return [job_id for job_id in jobs if chaos.job(job_id)["status"] == "needs_approval"]


def wait_until_told(
    chaos: Chaos,
    jobs: list[str],
    *,
    timeout: float,
    meanwhile: Callable[[], None] = lambda: None,
) -> list[str]:
    """Wait until every job waits on a question that reached the receiver.

    Returns what is still missing at the timeout, empty when nothing is: the
    caller asserts on it, so a run that never posts ends on that assertion.
    """
    deadline = time.monotonic() + timeout
    while True:
        meanwhile()
        waiting = set(parked(chaos, jobs))
        arrived = arrivals(chaos, jobs)
        # The open question of each waiting job: its last `approval_requested`.
        # Keys are in step order, so the last one per job is kept.
        open_question = {key.rsplit(":", 1)[0]: key for key in questions(chaos, list(waiting))}
        missing = sorted(
            [f"{job_id} (not waiting)" for job_id in jobs if job_id not in waiting]
            + [key for key in open_question.values() if key not in arrived]
        )
        if not missing or time.monotonic() > deadline:
            return missing
        time.sleep(0.2)


def answer_all(chaos: Chaos, jobs: list[str], *, timeout: float) -> list[str]:
    """Approve each open question once it arrived, until every job is final.

    Returns the jobs that were not final at the timeout.
    """
    deadline = time.monotonic() + timeout
    while True:
        jobs_now = {job_id: chaos.job(job_id) for job_id in jobs}
        left = [job_id for job_id, job in jobs_now.items() if job["status"] not in FINAL]
        if not left or time.monotonic() > deadline:
            return left
        arrived = arrivals(chaos, left)
        for job_id in left:
            if jobs_now[job_id]["status"] != "needs_approval":
                continue
            asked = chaos.question(job_id)
            if f"{job_id}:{asked['seq']}" in arrived:
                chaos.answer(job_id, "approve", asked)
        time.sleep(0.2)


def judge(
    chaos: Chaos,
    jobs: list[str],
    *,
    exactly_once: bool,
    arrived: dict[str, list[datetime]] | None = None,
) -> tuple[list[str], Counter[str]]:
    """What is wrong by the pass line of G12, and how often each question arrived.

    `arrived` is the receiver's log read earlier; without it, the log is read now.
    """
    asked = questions(chaos, jobs)
    arrived = arrivals(chaos, jobs) if arrived is None else arrived
    counts = Counter({key: len(arrived.get(key, [])) for key in asked})
    problems = [f"never arrived: {key}" for key, count in sorted(counts.items()) if count == 0]
    problems += [f"arrived but never asked: {key}" for key in sorted(set(arrived) - set(asked))]
    if exactly_once:
        problems += [
            f"arrived {count} times: {key}" for key, count in sorted(counts.items()) if count > 1
        ]
    for job_id in jobs:
        job = chaos.job(job_id)
        if job["status"] != "succeeded":
            problems.append(text_safe(f"{job_id} is {job['status']} ({job['error']})"))
    return problems, counts


def repeats(counts: Counter[str]) -> int:
    return sum(count - 1 for count in counts.values() if count > 1)


def test_every_question_reaches_the_receiver_exactly_once_when_nothing_fails(
    chaos: Chaos,
) -> None:
    chaos.workers_up()
    jobs = submit(chaos, JOBS)
    missing = wait_until_told(chaos, jobs, timeout=PARK_WITHIN)
    assert missing == [], f"no question reached the receiver for: {missing}"
    time.sleep(HOLD)
    left = answer_all(chaos, jobs, timeout=PARK_WITHIN)
    problems, counts = judge(chaos, jobs, exactly_once=True)
    print(f"\n{JOBS} jobs, {len(counts)} questions, {repeats(counts)} repeats")
    record(
        "G12 no kill",
        {
            "jobs": JOBS,
            "scenario": SCENARIO,
            "lease_seconds": SHORT_LEASE,
            "questions": len(counts),
            "arrivals": sum(counts.values()),
            "repeats": repeats(counts),
            "held_seconds": HOLD,
            "takeovers": sum(chaos.job(job_id)["takeovers"] for job_id in jobs),
        },
    )
    assert left == [], f"jobs not final: {left}"
    assert problems == [], "\n".join(problems)
    # More than one per job when a takeover after the answer asks again.
    assert len(counts) >= JOBS


def test_every_question_reaches_the_receiver_when_workers_are_killed(chaos: Chaos) -> None:
    chaos.workers_up()
    rng = random.Random(f"{SEED}:notify")  # noqa: S311
    kills = 0
    next_kill = time.monotonic() + rng.uniform(*KILL_EVERY)

    def kill_a_worker() -> None:
        """Kill a running worker, at random, when its time has come, and start it again."""
        nonlocal kills, next_kill
        if time.monotonic() < next_kill:
            return
        running = sorted(c for c, state in chaos.workers().items() if state == "running")
        if running:
            victim = rng.choice(running)
            docker("docker", "kill", victim, check=False)
            docker("docker", "start", victim, check=False)
            kills += 1
        next_kill = time.monotonic() + rng.uniform(*KILL_EVERY)

    # Every POST of this run is held, so that a kill can land inside one.
    chaos.receiver.delete("/notifications").raise_for_status()
    hold = [{"hang": HANG} for _ in range(JOBS * 3)]
    chaos.receiver.post("/_fake/answers", json={"answers": hold}).raise_for_status()
    jobs: list[str] = []
    try:
        jobs = submit(chaos, JOBS)
        missing = wait_until_told(chaos, jobs, timeout=PARK_WITHIN, meanwhile=kill_a_worker)
        chaos.start_stopped_workers()
        assert missing == [], f"no question reached the receiver for: {missing}"
        time.sleep(HOLD)
        left = answer_all(chaos, jobs, timeout=PARK_WITHIN)
    finally:
        chaos.start_stopped_workers()
        # Read before the clear below, which forgets every arrival.
        seen = arrivals(chaos, jobs)
        # The held answers that are left would slow the tests after this one.
        chaos.receiver.delete("/notifications").raise_for_status()
    problems, counts = judge(chaos, jobs, exactly_once=False, arrived=seen)
    takeovers = sum(chaos.job(job_id)["takeovers"] for job_id in jobs)
    print(
        f"\n{JOBS} jobs, seed {SEED}, {WORKERS} workers, {kills} kills, {len(counts)} questions,"
        f" {repeats(counts)} repeats"
    )
    record(
        "G12 random kills",
        {
            "jobs": JOBS,
            "seed": SEED,
            "scenario": SCENARIO,
            "lease_seconds": SHORT_LEASE,
            "receiver_hang_seconds": HANG,
            "kills": kills,
            "takeovers": takeovers,
            "questions": len(counts),
            "arrivals": sum(counts.values()),
            "repeats": repeats(counts),
        },
    )
    assert left == [], f"jobs not final: {left}"
    assert problems == [], "\n".join(problems)
    # A run in which no kill landed inside a POST shows no repeat, and so
    # proves nothing about a kill between the claim and the record.
    assert repeats(counts) >= 1, "no question arrived twice: no kill landed inside a POST"


def test_every_question_reaches_the_receiver_after_it_was_down(chaos: Chaos) -> None:
    chaos.workers_up()
    receiver = docker("docker", "compose", "ps", "--all", "--quiet", "fake-receiver").strip()
    assert receiver, "no fake-receiver container"
    # Stopped before the jobs start: a stop wipes the receiver's log.
    docker("docker", "stop", receiver)
    down = time.monotonic()
    returned: datetime | None = None
    try:
        jobs = submit(chaos, JOBS)
        deadline = down + DOWN
        while len(parked(chaos, jobs)) < JOBS and time.monotonic() < deadline:
            time.sleep(0.2)
        time.sleep(max(0.0, deadline - time.monotonic()))
        statuses = {job_id: chaos.job(job_id)["status"] for job_id in jobs}
        returned = datetime.now(UTC)
        docker("docker", "start", receiver)
    finally:
        if returned is None:
            docker("docker", "start", receiver, check=False)
        wait_for_the_receiver(chaos)
    ended = sorted(job_id for job_id, status in statuses.items() if status in FINAL)
    not_parked = sorted(job_id for job_id, status in statuses.items() if status != "needs_approval")
    # Waits past the pass line, so a late arrival is judged late, not missing.
    missing = wait_until_told(chaos, jobs, timeout=2 * AFTER_RETURN)
    arrived = arrivals(chaos, jobs)
    late = {
        key: round((times[0] - returned).total_seconds(), 3)
        for key, times in arrived.items()
        if (times[0] - returned).total_seconds() > AFTER_RETURN
    }
    slowest = max(((t[0] - returned).total_seconds() for t in arrived.values()), default=None)
    left = answer_all(chaos, jobs, timeout=PARK_WITHIN)
    problems, counts = judge(chaos, jobs, exactly_once=False)
    print(
        f"\n{JOBS} jobs, receiver down {DOWN:g} s, slowest first arrival"
        f" {slowest} s after its return, {repeats(counts)} repeats"
    )
    record(
        "G12 receiver down",
        {
            "jobs": JOBS,
            "scenario": SCENARIO,
            "lease_seconds": SHORT_LEASE,
            "down_seconds": DOWN,
            "questions": len(counts),
            "arrivals": sum(counts.values()),
            "repeats": repeats(counts),
            "slowest_seconds_after_return": slowest,
            "takeovers": sum(chaos.job(job_id)["takeovers"] for job_id in jobs),
        },
    )
    assert ended == [], f"jobs final while the receiver was down: {ended}"
    assert not_parked == [], f"jobs not waiting when the receiver returned: {not_parked}"
    assert missing == [], f"no question reached the receiver for: {missing}"
    assert late == {}, f"arrived later than {AFTER_RETURN:g} s after the return: {late}"
    assert left == [], f"jobs not final: {left}"
    assert problems == [], "\n".join(problems)
