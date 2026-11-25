"""Record-shaped text in a tool result, on the stack of `make up`.

The job goes through the API to the stack's worker, on the fake provider.
Its `notes.txt` holds text shaped like the runtime's records, built by
`record_shaped` in `tests/unit/test_record_shaped_text.py`. Every call is
scripted by the `hostile-result` scenario, so the text causes none of them.

The `run_shell` call connects to `198.51.100.7`, outside the job's scope. Its
guard drops the packets and counts them. TEST-NET addresses are outside the
deny list and the stack's networks, and need no name lookup: a lookup would
go to Docker's DNS server, which is in the deny list.
"""

import importlib.util
import json
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx2
import pytest

from runtime.logs import text_safe

from .test_scope_in_stack import steps_of

TIMEOUT_SECONDS = 90
FINAL = {"succeeded", "failed", "cancelled"}
UNIT_TESTS = Path(__file__).resolve().parents[1] / "unit" / "test_record_shaped_text.py"


def unit_tests() -> ModuleType:
    """The unit tests' module, loaded by its path. `tests/unit` is not a package."""
    name = "record_shaped_text_unit_tests"
    spec = importlib.util.spec_from_file_location(name, UNIT_TESTS)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before it runs: the dataclasses in it look their module up.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


UNIT = unit_tests()
SCOPE_RECORD = UNIT.SCOPE_RECORD
SHELL = UNIT.SHELL
ISSUE = UNIT.ISSUE


def wait_for(api_url: str, job_id: str, statuses: set[str]) -> str:
    """The job's status once it is one of `statuses`, or the last one seen at the timeout."""
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while True:
        status: str = httpx2.get(f"{api_url}/jobs/{job_id}", timeout=10).json()["status"]
        if status in statuses or time.monotonic() > deadline:
            return status
        time.sleep(0.2)


def snapshot(api_url: str, fake_tracker_url: str, database_url: str, job_id: str) -> dict[str, Any]:
    """The job's status, its steps with their payloads, and the tracker's whole list."""
    status = httpx2.get(f"{api_url}/jobs/{job_id}", timeout=10).json()["status"]
    steps = [
        {"seq": row["seq"], "kind": row["kind"], "payload": json.loads(row["payload"])}
        for row in steps_of(database_url, job_id)
    ]
    issues = httpx2.get(f"{fake_tracker_url}/issues", timeout=10).json()
    return {"status": status, "steps": steps, "issues": issues}


@pytest.fixture(scope="module")
def stack_run(
    api_url: str, fake_provider_url: str, fake_tracker_url: str, stack_database_url: str
) -> dict[str, Any]:
    """Submit the job, take a snapshot when it parks or ends, approve if it parked, snapshot again.

    Asserts nothing. A job that never parks gets no approval: its first
    snapshot is taken at its final status.
    """
    spec = {
        "prompt": UNIT.PROMPT,
        "model": UNIT.RESULT,
        "files": {"notes.txt": UNIT.record_shaped(gated_id=ISSUE, next_id=SHELL)},
        "scope": UNIT.SCOPE,
        "needs_approval": ["create_issue"],
    }
    submitted = httpx2.post(f"{api_url}/jobs", json=spec, timeout=10)
    submitted.raise_for_status()
    job_id: str = submitted.json()["id"]

    wait_for(api_url, job_id, {"needs_approval", *FINAL})
    before = snapshot(api_url, fake_tracker_url, stack_database_url, job_id)
    if before["status"] == "needs_approval":
        asked = [step["seq"] for step in before["steps"] if step["kind"] == "approval_requested"]
        httpx2.post(
            f"{api_url}/jobs/{job_id}/approve", json={"question": asked[-1]}, timeout=10
        ).raise_for_status()
        wait_for(api_url, job_id, FINAL)
    after = snapshot(api_url, fake_tracker_url, stack_database_url, job_id)
    log = httpx2.get(
        f"{fake_provider_url}/_fake/requests", params={"user_id": job_id}, timeout=10
    ).json()
    kinds = [step["kind"] for step in after["steps"]]
    print(text_safe(f"\n{job_id} {before['status']} -> {after['status']} {kinds}"))
    return {"job_id": job_id, "before": before, "after": after, "log": log}


def payload_of(steps: list[dict[str, Any]], kind: str, tool_use_id: str) -> dict[str, Any]:
    return next(
        step["payload"]
        for step in steps
        if step["kind"] == kind and step["payload"]["tool_use_id"] == tool_use_id
    )


def test_text_in_a_result_does_not_widen_the_scope_of_a_guarded_call(
    stack_run: dict[str, Any],
) -> None:
    """The text holds a scope with the address the call connects to. The call gets the spec's."""
    scope = payload_of(stack_run["before"]["steps"], "tool_intent", SHELL)["scope"]

    assert scope == SCOPE_RECORD


def test_a_connect_outside_the_scope_is_dropped_and_counted(stack_run: dict[str, Any]) -> None:
    """The guard drops the connect to the address outside the scope, and counts the packets."""
    result = payload_of(stack_run["before"]["steps"], "tool_result", SHELL)

    assert result["is_error"] is True
    assert result["dropped_packets"] > 0


def test_text_in_a_result_does_not_open_the_gate_in_the_stack(stack_run: dict[str, Any]) -> None:
    """The text approves the gated call by its real id. The job still parks on its question."""
    before = stack_run["before"]
    asked = [
        s["payload"]["tool_use_id"] for s in before["steps"] if s["kind"] == "approval_requested"
    ]
    intents = [s["payload"]["tool_use_id"] for s in before["steps"] if s["kind"] == "tool_intent"]

    assert before["status"] == "needs_approval"
    assert asked == [ISSUE]
    assert ISSUE not in intents


def test_text_in_a_result_creates_no_issue_before_the_approval(stack_run: dict[str, Any]) -> None:
    """The tracker's own list, before the approval: no issue of the job."""
    job_id = stack_run["job_id"]
    issues = stack_run["before"]["issues"]
    ours = [issue for issue in issues if issue["operation_id"].startswith(f"{job_id}:")]

    assert ours == []


def test_the_approved_job_runs_to_the_end_of_its_script(stack_run: dict[str, Any]) -> None:
    """A control, not evidence about the text: once approved, the job runs to its end.

    Exactly one issue of the job, under the gated call's operation id, and
    the fake answered every turn of the script.
    """
    job_id = stack_run["job_id"]
    after = stack_run["after"]
    ours = [issue for issue in after["issues"] if issue["operation_id"].startswith(f"{job_id}:")]

    assert after["status"] == "succeeded"
    assert [issue["operation_id"] for issue in ours] == [f"{job_id}:{ISSUE}"]
    assert [entry["turn"] for entry in stack_run["log"]] == [0, 1, 2, 3]
