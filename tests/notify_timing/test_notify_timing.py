"""G12, the timed run: every first delivery arrives within 5 s of the park.

The stack of `make up` with the fake provider and no kill. 50 jobs ask before
`create_issue`. For each question, the receiver's time of its first arrival
is compared with the database's time of the park: the `status_changed` step
that moved the job to `needs_approval` right after the question. Both clocks
are the one machine's.

Recorded runs start from the Manual runs workflow on a GitHub runner
(ADR 0020). A run on a laptop whose clock is stepped proves nothing.
NOTIFY_TIMING_JOBS sets the number of jobs.
"""

import itertools
import json
import os
import platform
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS = REPO_ROOT / ".notify-timing" / "results.jsonl"
EXAMPLE = json.loads((REPO_ROOT / "examples" / "ci-triage.json").read_text())
SCENARIO = "ci-triage"
JOBS = int(os.environ.get("NOTIFY_TIMING_JOBS", "50"))
# The pass line of G12 (b), in seconds.
WITHIN = 5.0
# Seconds the run may take to park every job and post every question.
TIMEOUT = 300.0


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


def run(*args: str) -> str:
    done = subprocess.run(args, cwd=REPO_ROOT, capture_output=True, text=True, check=True)  # noqa: S603 - fixed program
    return done.stdout.strip()


def parks(api: httpx2.Client, job_id: str) -> dict[str, datetime]:
    """When the job was parked on each of its questions, by the question's id."""
    steps = api.get(f"/jobs/{job_id}/steps").raise_for_status().json()["steps"]
    found = {}
    for asked, moved in itertools.pairwise(steps):
        if (
            asked["kind"] == "approval_requested"
            and moved["kind"] == "status_changed"
            and moved["payload"].get("to") == "needs_approval"
        ):
            found[f"{job_id}:{asked['seq']}"] = at(moved["created_at"])
    return found


def first_arrivals(receiver: httpx2.Client, jobs: set[str]) -> dict[str, datetime]:
    """The receiver's time of the first arrival of each question of these jobs."""
    found: dict[str, datetime] = {}
    for arrival in receiver.get("/notifications").raise_for_status().json():
        body = arrival["body"]
        if isinstance(body, dict) and body.get("job_id") in jobs:
            found.setdefault(str(body["id"]), at(arrival["received_at"]))
    return found


def test_every_first_delivery_arrives_within_5_s_of_the_park() -> None:
    api = httpx2.Client(
        base_url=os.environ.get("RUNTIME_API_URL", "http://127.0.0.1:8000"), timeout=10
    )
    receiver = httpx2.Client(
        base_url=os.environ.get("FAKE_RECEIVER_URL", "http://127.0.0.1:8003"), timeout=10
    )
    jobs = []
    for _ in range(JOBS):
        response = api.post(
            "/jobs", json={**EXAMPLE, "model": SCENARIO, "needs_approval": ["create_issue"]}
        )
        jobs.append(response.raise_for_status().json()["id"])

    parked: dict[str, datetime] = {}
    arrived: dict[str, datetime] = {}
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        parked = {k: v for job_id in jobs for k, v in parks(api, job_id).items()}
        arrived = first_arrivals(receiver, set(jobs))
        if len({key.rsplit(":", 1)[0] for key in parked}) == JOBS and set(parked) <= set(arrived):
            break
        time.sleep(0.5)

    delays = {key: (arrived[key] - parked[key]).total_seconds() for key in parked if key in arrived}
    missing = sorted(set(parked) - set(arrived))
    late = {key: round(delay, 3) for key, delay in delays.items() if delay > WITHIN}
    workers = run("docker", "compose", "ps", "--quiet", "worker").split()
    ordered = sorted(delays.values())
    result: dict[str, Any] = {
        "test": "G12 timed run",
        "commit": run("git", "rev-parse", "--short", "HEAD"),
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "stack": "make up",
        "workers": len(workers),
        "provider": "fake provider",
        "machine": f"{platform.system()} {platform.machine()}, {os.cpu_count()} CPUs",
        "where": "GitHub Actions" if os.environ.get("GITHUB_ACTIONS") else "local Docker Compose",
        "jobs": JOBS,
        "questions": len(parked),
        "delivered": len(delays),
        "median_seconds": round(ordered[len(ordered) // 2], 3) if ordered else None,
        "slowest_seconds": round(ordered[-1], 3) if ordered else None,
        "late": len(late),
        "verdict": "passed" if not missing and not late and len(parked) >= JOBS else "failed",
    }
    RESULTS.parent.mkdir(exist_ok=True)
    with RESULTS.open("a", encoding="utf-8") as file:
        file.write(json.dumps(result) + "\n")
    print(f"\nRESULT {json.dumps(result)}")

    # The jobs end, so the stack is left with nothing that waits.
    for job_id in jobs:
        for key in parks(api, job_id):
            api.post(f"/jobs/{job_id}/approve", json={"question": int(key.rsplit(":", 1)[1])})
    api.close()
    receiver.close()

    assert len({key.rsplit(":", 1)[0] for key in parked}) == JOBS, "not every job was parked"
    assert missing == [], f"never arrived: {missing}"
    assert late == {}, f"arrived later than {WITHIN:g} s after the park: {late}"
