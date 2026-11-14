"""G3: a worker that was paused past its lease cannot corrupt the job. Also a real SIGTERM.

Both tests run with the default lease of 30 seconds, as a normal stack has.

The job is the CI triage workload with two answers that take ten seconds
each. That is long enough to pause the worker while it waits for the first
answer, and to wake it while another worker is in the middle of the job.
"""

import time
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from .conftest import DEFAULT_LEASE, Chaos, docker, record

SCENARIO = "ci-triage-stall"
PAUSE_SECONDS = 45.0


@pytest.fixture(scope="module")
def default_lease(chaos: Chaos) -> Iterator[Chaos]:
    chaos.workers_up(lease=DEFAULT_LEASE)
    try:
        yield chaos
    finally:
        chaos.workers_up()


def waiting_for_the_first_answer(chaos: Chaos, job_id: str) -> str:
    """Wait until a worker has sent the first model request, and return its container."""
    chaos.wait_for(job_id, lambda job: job["lease_owner"] is not None, timeout=30, what="a claim")
    deadline = time.monotonic() + 30
    while not chaos.steps(job_id):
        assert time.monotonic() < deadline, "no model_request step"
        time.sleep(0.05)
    holder = chaos.holder(job_id)
    assert holder is not None
    return holder


def test_a_worker_paused_past_its_lease_writes_nothing_when_it_wakes(default_lease: Chaos) -> None:
    chaos = default_lease
    started = datetime.now(UTC)
    job_id = chaos.submit(SCENARIO)
    stale = waiting_for_the_first_answer(chaos, job_id)
    docker("docker", "pause", stale)
    paused_at = time.monotonic()
    try:
        taken = chaos.wait_for(
            job_id,
            lambda job: job["lease_epoch"] == 2,
            timeout=PAUSE_SECONDS,
            what="a takeover",
        )
        taken_after = time.monotonic() - paused_at
        time.sleep(max(0.0, PAUSE_SECONDS - (time.monotonic() - paused_at)))
        # The second worker is in the middle of the job when the first wakes.
        at_unpause = chaos.job(job_id)
    finally:
        docker("docker", "unpause", stale)
    woke_at = datetime.now(UTC)
    run = chaos.finish(job_id, timeout=60, note=f"paused {PAUSE_SECONDS:g}s")
    # Give the woken worker time to do whatever it is going to do.
    time.sleep(5)
    run = chaos.read(job_id, note=run.note)
    stale_log = chaos.logs(stale, started)
    after_waking = chaos.logs(stale, woke_at)
    print(f"\n{run.summary()} takeover after {taken_after:.1f}s")

    # The takeover came when the lease ran out, not before.
    assert taken_after >= DEFAULT_LEASE - 2
    assert taken["lease_owner"] is not None
    assert not taken["lease_owner"].startswith(stale[:12])
    assert at_unpause["status"] == "running"
    assert at_unpause["lease_epoch"] == 2

    # The job finished under the second worker.
    assert run.job["status"] == "succeeded"
    assert (run.job["takeovers"], run.job["lease_epoch"]) == (1, 2)

    # The step log: the stale worker's only step is the request it wrote
    # before the pause. Everything after it carries the second worker's epoch.
    assert [(s["kind"], s["lease_epoch"]) for s in run.steps][:2] == [
        ("model_request", 1),
        ("model_request", 2),
    ]
    assert {s["lease_epoch"] for s in run.steps[1:]} == {2}

    # The stale worker's own log: it found its lease gone and dropped the
    # job. It wrote no step and started no tool after waking.
    assert f'"job_id": "{job_id}"' in stale_log
    assert "job dropped: the lease is gone" in after_waking
    assert '"message": "step"' not in after_waking
    assert "failpoint" not in stale_log

    # The provider: one request from the stale worker, three from the second.
    # None came after the wake-up from a worker that had lost the job.
    assert run.turns == [0, 0, 1, 2]
    # The tracker: one issue. The stale worker opened none.
    assert [len(ids) for ids in run.issues.values()] == [1]
    record(
        "G3 paused worker",
        {
            "runs": 1,
            "scenario": SCENARIO,
            "lease_seconds": DEFAULT_LEASE,
            "pause_seconds": PAUSE_SECONDS,
            "takeover_after_seconds": round(taken_after, 1),
            "stale_steps_after_takeover": 0,
            "issues": 1,
        },
    )


def test_a_worker_stopped_with_sigterm_hands_its_job_back(default_lease: Chaos) -> None:
    """`docker stop` sends SIGTERM. The job must not wait for the lease to run out."""
    chaos = default_lease
    started = datetime.now(UTC)
    job_id = chaos.submit(SCENARIO)
    holder = waiting_for_the_first_answer(chaos, job_id)
    stop_began = time.monotonic()
    docker("docker", "stop", "--time", "30", holder)
    stop_took = time.monotonic() - stop_began
    try:
        handed_back = chaos.wait_for(
            job_id,
            lambda job: job["lease_epoch"] == 2 or job["status"] == "queued",
            timeout=10,
            what="another claim",
        )
        reclaimed_after = time.monotonic() - stop_began
        run = chaos.finish(job_id, timeout=90, note="SIGTERM")
    finally:
        docker("docker", "start", holder)
    stopped_log = chaos.logs(holder, started)
    print(f"\n{run.summary()} stop took {stop_took:.1f}s")

    # The worker stopped at once: it abandoned the model call it was waiting for.
    assert stop_took < 10
    # Another worker had the job long before the 30 s lease could have run out.
    assert reclaimed_after < 15
    assert handed_back["takeovers"] == 0
    assert "job handed back" in stopped_log
    assert run.job["status"] == "succeeded"
    assert (run.job["takeovers"], run.job["lease_epoch"]) == (0, 2)
    assert [len(ids) for ids in run.issues.values()] == [1]
    assert run.turns == [0, 0, 1, 2]
    record(
        "graceful stop (SIGTERM)",
        {
            "runs": 1,
            "scenario": SCENARIO,
            "lease_seconds": DEFAULT_LEASE,
            "stop_seconds": round(stop_took, 1),
            "takeovers": 0,
        },
    )
