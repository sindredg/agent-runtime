"""G4: a job that takes its worker down every time does not loop for ever.

The job calls the crash_worker tool, which ends the worker's process. Each
time, the lease runs out and another worker takes the job over. The claim
after the fifth takeover fails the job instead.

The tool writes a line to its own log file before each crash. That file, not
the step log, says how often the tool really ran.

It runs on the longer failpoint lease. A worker that loses its lease for
another reason also counts as a takeover, and runs nothing: on the 2 s lease
a slow renewal on a loaded machine did that once, and the tool ran five
times, not six.
"""

import json
from datetime import UTC, datetime

from .conftest import CHAOS_DIR, FAILPOINT_LEASE, Chaos, lease_trouble, record


def crash_log(job_id: str) -> list[dict[str, str]]:
    log = CHAOS_DIR / "crash_worker.log"
    if not log.exists():
        return []
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    return [entry for entry in entries if entry["operation_id"].startswith(f"{job_id}:")]


def test_a_job_that_crashes_its_worker_every_time_is_failed_after_five_takeovers(
    chaos: Chaos,
) -> None:
    chaos.workers_up(lease=FAILPOINT_LEASE)
    started = datetime.now(UTC)
    job_id = chaos.submit("crash-loop")
    try:
        run = chaos.finish(job_id, timeout=180, note="crash loop")
        logs = chaos.worker_logs(started)
    finally:
        chaos.start_stopped_workers()
        chaos.workers_up()
    runs = crash_log(job_id)
    print(f"\n{run.summary()} crash_worker runs={len(runs)}")

    # Before the checks: a failed run is recorded too, with its verdict.
    record(
        "G4 crash loop",
        {
            "runs": 1,
            "scenario": "crash-loop",
            "lease_seconds": FAILPOINT_LEASE,
            "takeovers": run.job["takeovers"],
            "tool_runs": len(runs),
            "status": run.job["status"],
        },
    )

    assert run.job["status"] == "failed"
    assert run.job["takeovers"] == 5
    assert "taken over 5 times" in run.job["error"]
    # The first claim and five takeovers: six runs of the tool, by its own log.
    # A miss names the workers that lost the lease without a crash.
    assert len(runs) == 6, (
        f"{len(runs)} tool runs for {run.job['takeovers']} takeovers;"
        f" lease lines: {lease_trouble(logs, job_id) or 'none'}"
    )
    assert len({entry["operation_id"] for entry in runs}) == 1
    # Each run has its own intent, and the log ends with the reason.
    kinds = [step["kind"] for step in run.steps]
    assert kinds.count("tool_intent") == 6
    assert kinds.count("tool_result") == 0
    assert kinds[-1] == "error"
    assert run.steps[-1]["payload"] == {"message": run.job["error"]}
    assert run.approvals == {}
