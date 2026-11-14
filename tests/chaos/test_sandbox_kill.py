"""A worker killed during a sandboxed call (ADR 0008).

Its container goes on without it. The worker that takes the job over removes
the container, and asks a person about the call only once the call's bound
has passed since its intent. `run_shell` is not idempotent, so a person is
asked; the runner approves, and the command runs again.

What happened is read from Docker and from the step log's database times.
"""

import time
from datetime import datetime

from .conftest import Chaos, docker, record

SCENARIO = "sandbox-kill"


def containers(job_id: str) -> dict[str, tuple[int, bool]]:
    """The job's containers, running or not: id -> (lease epoch, running)."""
    out = docker(
        "docker", "ps", "--all", "--filter", f"label=agent-runtime.job={job_id}",
        "--format", '{{.ID}} {{.Label "agent-runtime.lease-epoch"}} {{.State}}',
    )  # fmt: skip
    found = {}
    for line in out.splitlines():
        container, epoch, state = line.split()
        found[container] = (int(epoch), state == "running")
    return found


def at(step: dict[str, object]) -> datetime:
    return datetime.fromisoformat(str(step["created_at"]))


def test_a_kill_during_a_sandboxed_call_removes_the_container_and_waits(chaos: Chaos) -> None:
    chaos.workers_up()
    job_id = chaos.submit(SCENARIO)

    deadline = time.monotonic() + 60
    while not any(running for _, running in containers(job_id).values()):
        assert time.monotonic() < deadline, "the command's container never ran"
        time.sleep(0.05)
    victim = chaos.holder(job_id)
    assert victim is not None
    docker("docker", "kill", victim)
    killed = containers(job_id)
    docker("docker", "start", victim)

    job = chaos.wait_for(
        job_id, lambda j: j["status"] == "needs_approval", timeout=120, what="needs_approval"
    )
    left = containers(job_id)
    steps = chaos.steps(job_id)
    intent = next(s for s in steps if s["kind"] == "tool_intent")
    question = next(s for s in steps if s["kind"] == "approval_requested")
    waited = (at(question) - at(intent)).total_seconds()
    bound = intent["payload"]["ends_within"]

    run = chaos.finish(job_id, note="kill during run_shell")
    print(f"\n{run.summary()} waited={waited:.1f}s bound={bound}")
    record(
        "kill during a sandboxed call",
        {
            "scenario": SCENARIO,
            "lease_seconds": chaos.lease,
            "bound_seconds": bound,
            "waited_seconds": round(waited, 3),
            "containers_at_the_kill": len(killed),
            "containers_of_the_killed_worker_left": len(left),
            "status": run.job["status"],
        },
    )

    # The kill left a running container of the first lease.
    assert [epoch for epoch, running in killed.values() if running] == [1]
    # The worker that took over removed it, before it asked anything.
    assert job["lease_epoch"] >= 2
    assert left == {}
    # It decided only after the bound, by the database clock.
    assert waited >= bound
    # The person approved, the command ran again, and the job finished.
    assert run.job["status"] == "succeeded"
    assert sum(run.approvals.values()) == 1
    results = [s for s in run.steps if s["kind"] == "tool_result"]
    assert len(results) == 1
    assert results[0]["payload"]["content"] == "started\nfinished\n[exit status 0]"
