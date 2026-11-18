"""The lab demo: a worker stops in the middle of a call that opens a Gitea issue.

The job reads a CI log and opens an issue. The worker that runs it ends its
own process at a failpoint, as a kill would. Another worker takes the job
over, finds the unfinished call in the step log, and asks Gitea whether the
issue is there before it decides (ADR 0005, ADR 0019).

The pass line: the job succeeds, and Gitea's own list holds exactly one
issue for the call.
"""

import io
import json
from datetime import UTC, datetime

import pytest

from runtime.cli import main as runtime_cli
from runtime.tools.base import operation_id

from .conftest import DATABASE_URL, FIRED, REPO_ROOT, Lab, say

JOB = json.loads((REPO_ROOT / "examples" / "ci-triage.json").read_text())

CASES = {
    "after_tool_ran": "Gitea has the issue, the result was not stored",
    "after_tool_intent": "the intent is stored, the request was never sent",
}


@pytest.mark.parametrize("failpoint", list(CASES))
def test_a_worker_stopped_in_a_call_to_gitea_leaves_exactly_one_issue(
    lab: Lab, failpoint: str
) -> None:
    print(f"\n\nCase {failpoint}: the worker stops where {CASES[failpoint]}.", flush=True)
    lab.workers_up(failpoint)
    started = datetime.now(UTC)
    job_id = lab.submit(JOB)
    say(f"submitted job {job_id} (ci-triage, fake provider)")
    job = lab.finish(job_id)
    logs = lab.worker_logs(started)
    steps = lab.steps(job_id)

    fired = logs.count(FIRED)
    say(f"a worker stopped at {failpoint}: {fired} time(s); takeovers: {job['takeovers']}")
    intent = next(
        s for s in steps if s["kind"] == "tool_intent" and s["payload"]["name"] == "create_issue"
    )
    call = intent["payload"]["tool_use_id"]
    operation = operation_id(job_id, call)
    results = [
        s for s in steps if s["kind"] == "tool_result" and s["payload"]["tool_use_id"] == call
    ]
    for result in results:
        how = (
            "from the check, the tool did not run"
            if result["payload"].get("recovered")
            else ("the tool ran")
        )
        say(f"epoch {result['lease_epoch']}: {result['payload']['content']!r} ({how})")
    issues = lab.issues_naming(operation)
    say(f"Gitea holds {len(issues)} issue(s) for {operation}: {[i['number'] for i in issues]}")
    out, err = io.StringIO(), io.StringIO()
    verified = runtime_cli(["verify", "--database-url", DATABASE_URL, job_id], out=out, err=err)
    say(f"runtime verify: exit {verified}, {out.getvalue().strip() or err.getvalue().strip()}")
    say(f"job {job['status']}")

    # The stop landed: without it the run shows nothing.
    assert fired == 1, f"the failpoint fired {fired} times"
    assert job["takeovers"] >= 1
    assert job["status"] == "succeeded"
    assert len(issues) == 1, f"Gitea holds {len(issues)} issues for {operation}"
    assert len(results) == 1
    recovered = results[0]["payload"].get("recovered") is True
    assert recovered is (failpoint == "after_tool_ran")
    assert verified == 0
