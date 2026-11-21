"""Switched-off runs: remove one mechanism, run the tests that guard it, expect them to fail.

A guarantee is "tested" only with a run that removes its mechanism and fails
its test (README). Each entry below names the edit, the tests, and how each
test must end: failed on a named assertion, or passed for a stated reason.
The list is fixed before a run. A test that ends otherwise fails the run, and
is a finding for the results page, not a reason to change the entry.

The Manual runs workflow runs one entry, or every entry of the fake provider
with one job each, on `main` (ADR 0021). An entry names its provider. One on
the Claude API runs in the job that holds the key, and is judged by the kill
runner's record as well (ADR 0022).

    python scripts/switched_off.py names          the entries, one per line
    python scripts/switched_off.py names fake     the entries of the fake provider, as a
                                                  JSON list: the workflow's matrix
    python scripts/switched_off.py check          the list still fits the code
    python scripts/switched_off.py dispatch NAME PROVIDER [MODEL]
                                                  refuse a start that is not the entry's
    python scripts/switched_off.py run NAME       edit the checkout, start the stack, run, judge
    python scripts/switched_off.py rehearse NAME  an entry of the Claude API on the fake
                                                  provider: every step of `run`, and no result

`run` and `rehearse` change files in the checkout. Run them on a clean
checkout and restore it with `git checkout -- .` afterwards.

`run` starts an entry of the Claude API only with SWITCHED_OFF_PROVIDER=claude.
Every job then costs money.
"""

import importlib.util
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Literal

ROOT = Path(__file__).resolve().parents[1]
JUNIT = Path(".switched-off/junit.xml")
WORKFLOW = ".github/workflows/manual-runs.yml"
AGENT = "src/runtime/worker/agent.py"
SANDBOXED = "src/runtime/tools/sandboxed.py"
WORKSPACE = "src/runtime/sandbox/workspace.py"
RUNNER = "src/runtime/sandbox/runner.py"
CHAIN_MIGRATION = "migrations/versions/0007_step_log_chain.py"
KILLS = "tests/chaos/test_kills.py"
FAILPOINT = f"{KILLS}::test_a_job_survives_its_worker_stopping_at_the_failpoint"
HALT = "tests/integration/test_halt_store.py"
SANDBOX = "tests/integration/test_sandbox.py"
WATCHER = "tests/unit/test_workspace_watcher.py"
APPROVAL = "tests/integration/test_approval.py"
CHAOS_APPROVAL = "tests/chaos/test_approval.py"
BACKOFF_WORKER = "tests/integration/test_backoff_worker.py"
BUSY = f"{BACKOFF_WORKER}::test_a_busy_provider_makes_the_job_wait_and_then_finish"
BUSY_CASES = ("[429-rate_limit_error]", "[529-overloaded_error]")
IN_STACK = "tests/integration/test_backoff_in_stack.py::test_a_rate_limited_job_waits_and_succeeds"
CHAIN = "tests/integration/test_step_log_chain.py"
VERIFY = "tests/integration/test_verify_command.py"
# The cases of TAMPERED in the chain's tests that only the hash comparison finds.
HASH_CASES = ("change", "change the epoch", "change and hash again")
ORDER_CASES = (
    "delete",
    "reorder",
    "insert out of order",
    "cut the tail and hash the last step again",
)
LAB = (
    "tests/lab/test_lab_demo.py::test_a_worker_stopped_in_a_call_to_gitea_leaves_exactly_one_issue"
)
# The tests that a run with no stack, and the lab's stack, may name.
STACK_TESTS = {"none": "tests/unit/", "lab": "tests/lab/"}
# The kill runs: their rules, their tests, and where a session leaves its records.
KILL_RULES = "tests/kills/rules.py"
KILL_RUNS = "tests/kills/test_kill_runs.py"
KILL_RECORDS = ".kills"
# The most tokens the jobs of an entry on the Claude API may count: its jobs times
# the budget of each. What the API bills can be more (docs/limits.md, halt and budget).
KEYED_TOKEN_BOUND = 250_000
# `run` starts an entry of the Claude API only when this says `claude`.
PROVIDER_VARIABLE = "SWITCHED_OFF_PROVIDER"
# Never passed on to a run on the fake provider, nor to this script's own git commands.
KEY_VARIABLES = ("CLAUDE_API_KEY", "ANTHROPIC_API_KEY")
# The counts of a kill runner's record that a `Refusals` entry is judged by.
SENT = "jobs_that_sent_a_rebuilt_thinking_block"
ANSWERED = "jobs_answered_after_sending_one"
REFUSED = "jobs_refused_after_sending_one"
JUDGED = (SENT, ANSWERED, REFUSED, "failed_runs", "problems")


@dataclass(frozen=True)
class Edit:
    path: str
    # Must occur exactly once in the file.
    old: str
    new: str


@dataclass(frozen=True)
class Expect:
    node: str
    # Text the failure must carry: the assertion that guards the mechanism.
    # None: the test is expected to pass, for the reason in `passes_because`.
    fails_with: str | None
    passes_because: str = ""


@dataclass(frozen=True)
class Refusals:
    """What the kill runner's record of the test must show, besides the test's failure.

    Every job that sent a rebuilt `thinking` block was refused by the API for
    the block's signature, none was answered, and every other job passed.
    """

    # The `test` of the record.
    test: str
    # With fewer refused jobs than this the run shows too little: inconclusive.
    at_least: int


@dataclass(frozen=True)
class SwitchedOff:
    issue: int
    what: str
    edits: tuple[Edit, ...]
    # `none`: no stack, for unit tests. `up`: the normal stack. `chaos`: three
    # workers and a short lease. `kills`: the kill runs' stack, three workers
    # and a 6 s lease. `lab`: the images built, and the lab's tests start the
    # stack with Gitea themselves.
    stack: Literal["none", "up", "chaos", "kills", "lab"]
    expect: tuple[Expect, ...]
    # `claude`: the run is on the Claude API, in the job that holds the key.
    provider: Literal["fake", "claude"] = "fake"
    # The model of a run on the Claude API.
    model: str | None = None
    # The kills stack: the jobs of the test, and the most the run may submit.
    runs: int = 0
    record: Refusals | None = None


NOTHING_TO_REPLAY = (
    "the kill comes before any completed step, so starting over is the same as resuming"
)
REPLAY = "    state = replay(spec.prompt, steps)\n"
# On every claim: the first character of each stored signature. A block
# without a signature, and every other block, stays as it was stored.
CHANGED_SIGNATURES = (
    REPLAY
    + """\
    for message in state.messages:  # switched off
        content = message["content"]
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict) or block.get("type") != "thinking":
                continue
            signature = block.get("signature")
            if isinstance(signature, str) and signature:
                block["signature"] = ("B" if signature[0] == "A" else "A") + signature[1:]
"""
)

ENTRIES: dict[str, SwitchedOff] = {
    "budget-off": SwitchedOff(
        issue=116,
        what="the check that the next call fits and the cut of max_tokens",
        edits=(
            Edit(
                AGENT,
                "            max_tokens = max_tokens_within(job.token_budget, used, bound,"
                " settings.max_tokens)\n",
                "            max_tokens = settings.max_tokens  # switched off\n",
            ),
        ),
        stack="up",
        expect=(
            Expect(
                "tests/integration/test_budget.py"
                "::test_a_job_that_loops_stops_before_its_budget_by_the_providers_count",
                "spent <= BUDGET",
            ),
        ),
    ),
    "resume-off": SwitchedOff(
        issue=117,
        what="the replay of the step log on a claim",
        edits=(Edit(AGENT, REPLAY, "    state = replay(spec.prompt, [])  # switched off\n"),),
        stack="chaos",
        expect=(
            Expect(f"{FAILPOINT}[after_claim]", None, NOTHING_TO_REPLAY),
            Expect(f"{FAILPOINT}[after_model_request]", None, NOTHING_TO_REPLAY),
            Expect(f"{FAILPOINT}[mid_stream]", None, NOTHING_TO_REPLAY),
            Expect(f"{FAILPOINT}[after_tool_intent]", "verdict(run, uninterrupted)"),
            Expect(f"{FAILPOINT}[after_tool_ran]", "verdict(run, uninterrupted)"),
            Expect(f"{FAILPOINT}[after_tool_result]", "verdict(run, uninterrupted)"),
        ),
    ),
    "unknown-check-reruns": SwitchedOff(
        issue=118,
        what="asking a person when a check cannot tell (ADR 0005)",
        edits=(
            Edit(
                "src/runtime/worker/recovery.py",
                "        return Ask(\"the tool's check could not tell"
                ' whether the call happened")\n',
                '        return Rerun("switched off: an unknown check runs the call again")\n',
            ),
        ),
        stack="chaos",
        expect=(
            Expect(
                f"{KILLS}::test_a_call_nobody_can_vouch_for_waits_for_approval_and_then_runs_again",
                'parked["status"] == "needs_approval"',
            ),
        ),
    ),
    "claim-cancel-off": SwitchedOff(
        issue=119,
        what="the claim's cancel of a job whose cancel was requested",
        edits=(
            Edit(
                "src/runtime/store/jobs.py",
                "    cancelled = jobs.c.cancel_requested\n",
                "    cancelled = and_(jobs.c.cancel_requested, literal(False))  # switched off\n",
            ),
        ),
        stack="up",
        expect=(
            Expect(
                f"{HALT}::test_a_claim_cancels_a_running_job_whose_cancel_was_requested",
                "claimed is None",
            ),
        ),
    ),
    "overdue-timer-off": SwitchedOff(
        issue=119,
        what="the workers' timer that fails jobs waiting past their deadline",
        edits=(
            Edit(
                "src/runtime/worker/loop.py",
                "                await fail_overdue(conn)\n",
                "                pass  # switched off\n",
            ),
        ),
        stack="up",
        expect=(
            Expect(
                f"{HALT}::test_the_workers_timer_fails_a_parked_job_within_seconds_of_its_deadline",
                "the timer never failed the job",
            ),
        ),
    ),
    "stop-release-off": SwitchedOff(
        issue=120,
        what="the hand-back of a job when its worker is told to stop",
        edits=(
            Edit(
                AGENT,
                "            return JobStatus.QUEUED\n",
                "            raise LeaseLost(job.id, lease.epoch)  # switched off\n",
            ),
        ),
        stack="chaos",
        expect=(
            Expect(
                "tests/chaos/test_stale_worker.py"
                "::test_a_worker_stopped_with_sigterm_hands_its_job_back",
                "another claim",
            ),
        ),
    ),
    "takeover-outlast-off": SwitchedOff(
        issue=120,
        what="the removal of an earlier lease's container, and the wait, after a takeover",
        edits=(
            Edit(
                AGENT,
                "    bound = _bound(tool, intent)\n",
                "    bound = None  # switched off\n",
            ),
        ),
        stack="chaos",
        expect=(
            Expect(
                "tests/chaos/test_sandbox_kill.py"
                "::test_a_kill_during_a_sandboxed_call_removes_the_container_and_waits",
                "left == {}",
            ),
        ),
    ),
    "thinking-signature-changed": SwitchedOff(
        issue=122,
        what="sending a stored `thinking` block back as it was stored",
        edits=(Edit(AGENT, REPLAY, CHANGED_SIGNATURES),),
        stack="kills",
        expect=(
            Expect(
                f"{KILL_RUNS}::test_jobs_survive_their_worker_stopping_inside_the_call"
                "[after_tool_ran]",
                "assert not failures",
            ),
        ),
        provider="claude",
        model="claude-fable-5-2",
        runs=5,
        record=Refusals(test="failpoint after_tool_ran", at_least=3),
    ),
    # Durability
    "resume-off-random-kills": SwitchedOff(
        issue=158,
        what="the replay of the step log on a claim, with workers killed at random times",
        edits=(Edit(AGENT, REPLAY, "    state = replay(spec.prompt, [])  # switched off\n"),),
        stack="chaos",
        expect=(
            Expect(f"{KILLS}::test_jobs_survive_a_kill_at_a_random_time", "assert not failures"),
        ),
    ),
    "intent-write-off-chaos": SwitchedOff(
        issue=158,
        what="the step that records a tool call's intent before it runs",
        edits=(
            Edit(
                AGENT,
                "    await writer.append(StepKind.TOOL_INTENT, intent)\n",
                "    pass  # switched off: no intent\n",
            ),
        ),
        stack="chaos",
        expect=(
            Expect(
                f"{FAILPOINT}[after_tool_intent]",
                'assert [s["lease_epoch"] for s in create] == [1, 2]',
            ),
            Expect(f"{FAILPOINT}[after_tool_ran]", "assert verdict(run, uninterrupted) == []"),
        ),
    ),
    "every-tool-idempotent": SwitchedOff(
        issue=158,
        what="the tools' own idempotency flags: every tool is treated as idempotent",
        edits=(
            Edit(
                "src/runtime/worker/recovery.py",
                "    if tool.idempotent and not gated:\n",
                "    if not gated:  # switched off\n",
            ),
        ),
        stack="chaos",
        expect=(
            Expect(f"{FAILPOINT}[after_tool_ran]", "assert verdict(run, uninterrupted) == []"),
        ),
    ),
    "epoch-check-off": SwitchedOff(
        issue=158,
        what="the lease epoch in the fence of a worker's writes",
        edits=(
            Edit(
                "src/runtime/store/held.py",
                "        jobs.c.lease_epoch == lease_epoch,\n",
                "        # switched off: no epoch check\n",
            ),
        ),
        stack="chaos",
        expect=(
            Expect(
                "tests/chaos/test_stale_worker.py"
                "::test_a_worker_paused_past_its_lease_writes_nothing_when_it_wakes",
                "assert stale_steps == []",
            ),
        ),
    ),
    "gitea-check-not-done": SwitchedOff(
        issue=158,
        what="the Gitea check of create_issue, which always says not done",
        edits=(
            Edit(
                "src/runtime/tools/create_issue.py",
                "        if found is None:\n            return CheckOutcome.NOT_DONE\n",
                "        return CheckOutcome.NOT_DONE  # switched off\n",
            ),
        ),
        stack="lab",
        expect=(Expect(f"{LAB}[after_tool_ran]", "assert len(issues) == 1"),),
    ),
    "issue-marker-off": SwitchedOff(
        issue=158,
        what="the marker line with the operation id in the issue body",
        edits=(
            Edit(
                "src/runtime/tools/trackers.py",
                '    return f"{body}\\n\\n{MARKER}{operation_id}"',
                "    return body  # switched off",
            ),
        ),
        stack="lab",
        expect=(
            Expect(f"{LAB}[after_tool_ran]", "assert len(issues) == 1"),
            Expect(f"{LAB}[after_tool_intent]", 'assert operation in issues[0]["body"]'),
        ),
    ),
    # The step log's hash chain
    "refusal-triggers-off": SwitchedOff(
        issue=158,
        what="the triggers that refuse a change to the step log",
        edits=(
            Edit(
                CHAIN_MIGRATION,
                '        "CREATE TRIGGER steps_no_change BEFORE UPDATE OR DELETE ON steps"\n'
                '        " FOR EACH ROW EXECUTE FUNCTION steps_refuse()"\n',
                '        "SELECT 1"  # switched off\n',
            ),
            Edit(
                CHAIN_MIGRATION,
                '        "CREATE TRIGGER steps_no_truncate BEFORE TRUNCATE ON steps"\n'
                '        " FOR EACH STATEMENT EXECUTE FUNCTION steps_refuse()"\n',
                '        "SELECT 1"  # switched off\n',
            ),
        ),
        stack="up",
        expect=tuple(
            Expect(
                f"{CHAIN}::test_the_owner_is_refused_a_change_while_the_triggers_are_on[{statement}]",
                'pytest.raises(asyncpg.IntegrityConstraintViolationError, match="append-only")',
            )
            for statement in (
                "UPDATE steps SET payload = '{\"changed\": true}' WHERE job_id = $1",
                "DELETE FROM steps WHERE job_id = $1 AND seq = 3",
                "TRUNCATE steps",
            )
        ),
    ),
    "chain-check-off": SwitchedOff(
        issue=158,
        what="the comparison of each step's hash in verify",
        edits=(
            Edit(
                "src/runtime/store/chain.py",
                "        if want != row.hash:\n",
                "        if False:  # switched off\n",
            ),
        ),
        stack="up",
        expect=(
            *(
                Expect(
                    f"{CHAIN}::test_a_change_the_owner_makes_is_found[{case}]",
                    "assert verify(database_url, job_id) == expected",
                )
                for case in (*HASH_CASES, *ORDER_CASES)
            ),
            *(
                Expect(
                    f"{VERIFY}::test_the_command_names_what_the_owner_changed[{case}]",
                    'assert (status, err) == (1, "")',
                )
                for case in HASH_CASES
            ),
            *(
                Expect(
                    f"{VERIFY}::test_the_command_names_what_the_owner_changed[{case}]",
                    'assert out.splitlines() == [*named, "1 jobs checked, 1 with a mismatch"]',
                )
                for case in ORDER_CASES
            ),
            Expect(
                f"{VERIFY}::test_a_job_that_cannot_be_checked_is_not_a_pass",
                "assert run(database_url, UNKNOWN, str(job_id))[0] == 1",
            ),
        ),
    ),
    # Budget, deadline and approval
    "budget-check-off": SwitchedOff(
        issue=158,
        what="the check that the next call fits the budget; the cut of max_tokens stays",
        edits=(
            Edit(
                AGENT,
                "            if max_tokens is None:\n",
                "            if max_tokens is None and False:  # switched off\n",
            ),
        ),
        stack="up",
        expect=(
            Expect(
                "tests/integration/test_budget.py"
                "::test_a_job_that_loops_stops_before_its_budget_by_the_providers_count",
                'job.error.startswith("the token budget does not cover the next model call")',
            ),
        ),
    ),
    "claim-deadline-off": SwitchedOff(
        issue=158,
        what="the claim's check of a job's deadline",
        edits=(
            Edit(
                "src/runtime/store/jobs.py",
                "    overdue = and_(~cancelled, jobs.c.deadline <= now)\n",
                "    overdue = and_(~cancelled, jobs.c.deadline < jobs.c.deadline)"
                "  # switched off\n",
            ),
        ),
        stack="up",
        expect=tuple(
            Expect(
                f"{HALT}::test_a_claim_fails_a_job_past_its_deadline_with_an_error_step[{held}]",
                "assert claimed is None",
            )
            for held in ("False", "True")
        ),
    ),
    "approval-gate-off": SwitchedOff(
        issue=158,
        what="the gate that asks a person before a tool call that needs approval",
        edits=(
            Edit(
                "src/runtime/worker/approval.py",
                "    if tool is not None and tool.needs_approval:\n"
                "        return \"the tool's calls need a person's approval before they run\"\n"
                "    if name in named:\n"
                "        return \"the job's spec asks for a person's approval before this tool"
                ' runs"\n'
                "    return None\n",
                "    return None  # switched off\n",
            ),
        ),
        stack="up",
        expect=(
            *(
                Expect(f"{APPROVAL}::{test}", "assert await person.work() is S.NEEDS_APPROVAL")
                for test in (
                    "test_a_call_that_needs_approval_does_not_run_before_a_person_approved_it",
                    "test_a_denied_call_never_runs_and_the_job_goes_on",
                    "test_a_spec_adds_a_tool_to_the_gate",
                    "test_an_approved_call_that_is_interrupted_is_asked_about_again[idempotent]",
                    "test_an_approved_call_that_is_interrupted_is_asked_about_again"
                    "[check-says-not-done]",
                    "test_an_approval_sent_again_after_the_job_asked_about_the_next_call_is_refused",
                    "test_an_approval_sent_again_after_the_same_call_was_asked_about_again_is_refused",
                    "test_an_approval_that_names_no_question_does_not_count",
                    "test_a_question_asked_before_the_worker_stopped_is_not_asked_twice",
                    "test_an_open_question_holds_on_a_worker_whose_tool_would_not_have_asked",
                    "test_an_approved_run_is_marked_so_that_any_worker_asks_again_after_it_is"
                    "_interrupted",
                    "test_a_job_nobody_answers_is_failed_at_its_deadline_and_the_call_never_runs",
                )
            ),
            Expect(
                f"{APPROVAL}::test_a_spec_cannot_take_a_tool_out_of_the_gate[empty-list]",
                "assert acts.runs == []",
            ),
            Expect(
                f"{APPROVAL}::test_an_answer_about_another_call_does_not_let_this_one_run",
                "assert status is S.NEEDS_APPROVAL",
            ),
        ),
    ),
    "approval-gate-off-chaos": SwitchedOff(
        issue=158,
        what="the gate that asks a person before a tool call, with workers killed",
        edits=(
            Edit(
                "src/runtime/worker/approval.py",
                "    if tool is not None and tool.needs_approval:\n"
                "        return \"the tool's calls need a person's approval before they run\"\n"
                "    if name in named:\n"
                "        return \"the job's spec asks for a person's approval before this tool"
                ' runs"\n'
                "    return None\n",
                "    return None  # switched off\n",
            ),
        ),
        stack="chaos",
        expect=(
            Expect(
                f"{CHAOS_APPROVAL}"
                "::test_a_gated_call_never_runs_before_its_approval_under_random_kills",
                "assert not failures",
            ),
            *(
                Expect(
                    f"{CHAOS_APPROVAL}"
                    f"::test_an_approval_covers_one_run_when_the_worker_stops_at_the_failpoint"
                    f"[{failpoint}]",
                    "assert gated.problems() == []",
                )
                for failpoint in ("after_tool_intent", "after_tool_ran")
            ),
            Expect(
                f"{CHAOS_APPROVAL}::test_a_gated_job_nobody_answers_is_failed_at_its_deadline",
                "assert len(failed_at) == 1",
            ),
        ),
    ),
    # A busy provider
    "busy-answer-fails": SwitchedOff(
        issue=158,
        what="the wait on a busy answer: a 429 or 529 fails the job",
        edits=(
            Edit(
                AGENT,
                "                if error.busy:\n",
                "                if False:  # switched off\n",
            ),
        ),
        stack="up",
        expect=(
            *(Expect(f"{BUSY}{case}", "assert job is not None") for case in BUSY_CASES),
            Expect(
                f"{BACKOFF_WORKER}::test_the_wait_counts_again_after_a_response",
                "assert status is JobStatus.WAITING",
            ),
            Expect(
                f"{BACKOFF_WORKER}::test_a_job_whose_cancel_is_requested_does_not_wait",
                "assert status is JobStatus.CANCELLED",
            ),
            Expect(IN_STACK, 'assert status == "succeeded"'),
        ),
    ),
    "retry-after-ignored": SwitchedOff(
        issue=158,
        what="the floor that the provider's retry-after sets on a wait",
        edits=(
            Edit(
                "src/runtime/worker/backoff.py",
                "    return max(retry_after or 0.0, ceiling(attempt) / 2)",
                "    return ceiling(attempt) / 2  # switched off",
            ),
        ),
        stack="up",
        expect=(
            Expect(
                "tests/unit/test_backoff.py::test_retry_after_raises_the_floor_and_never_lowers_it",
                "assert [floor(n, 5) for n in range(1, 7)] == [5, 5, 5, 8, 16, 30]",
            ),
            Expect(
                "tests/unit/test_backoff.py"
                "::test_the_pass_lines_bound_six_busy_answers_in_sixty_seconds",
                "assert starts == [0, 5, 10, 15, 23, 39, 69]",
            ),
            Expect(
                "tests/unit/test_backoff.py"
                "::test_a_delay_lies_between_its_floor_and_half_a_ceiling_above_it",
                "assert waited >= (retry_after or 0)",
            ),
            Expect(
                "tests/unit/test_backoff.py"
                "::test_the_random_part_spreads_jobs_that_were_refused_together",
                "assert all(5 <= value <= 9 for value in delays)",
            ),
            Expect(IN_STACK, "assert gap.total_seconds() >= floor(1, RETRY_AFTER) == RETRY_AFTER"),
        ),
    ),
    "claim-ignores-not-before": SwitchedOff(
        issue=158,
        what="the claim's wait for a waiting job's not_before",
        edits=(
            Edit(
                "src/runtime/store/jobs.py",
                "and_(jobs.c.status == waiting, jobs.c.not_before <= now),",
                "jobs.c.status == waiting,  # switched off",
            ),
        ),
        stack="up",
        expect=(
            Expect(
                "tests/integration/test_backoff_store.py"
                "::test_the_claim_leaves_a_waiting_job_until_its_not_before",
                "assert early is None",
            ),
            Expect(
                "tests/integration/test_backoff_store.py"
                "::test_a_waiting_job_without_not_before_is_not_claimed",
                "assert with_engine(database_url, body) is None",
            ),
            *(Expect(f"{BUSY}{case}", "assert job is not None") for case in BUSY_CASES),
            Expect(IN_STACK, "assert 1 <= len(refused) <= MOST_REFUSED"),
        ),
    ),
    # Bad tool output
    "stream-cut-off": SwitchedOff(
        issue=158,
        what="the cut of a command's output stream at the limit",
        edits=(
            Edit(
                "src/runtime/sandbox/output.py",
                "            room = self.limit - len(self.kept)\n",
                "            room = len(data)  # switched off\n",
            ),
        ),
        stack="up",
        expect=(
            Expect(
                f"{SANDBOX}::test_output_past_the_limit_is_counted_and_not_kept",
                "assert len(done.output) == LIMIT",
            ),
        ),
    ),
    "result-cut-off": SwitchedOff(
        issue=158,
        what="the cut of a tool result to its limit",
        edits=(
            Edit(
                SANDBOXED,
                "    if len(content.encode()) > limit:\n",
                "    if False:  # switched off: the result is not cut\n",
            ),
        ),
        stack="up",
        expect=(
            Expect(
                f"{SANDBOX}::test_bad_output_becomes_tool_results_and_the_job_goes_on",
                'assert len(flood["content"].encode()) <= LIMIT',
            ),
        ),
    ),
    "tool-timeout-off": SwitchedOff(
        issue=158,
        what="the timeout of a sandboxed command, in the runner and in the container",
        edits=(
            Edit(
                RUNNER,
                "                        async with asyncio.timeout(settings.timeout)"
                " as deadline:\n",
                "                        async with asyncio.timeout(None) as deadline:"
                "  # switched off\n",
            ),
            Edit(
                RUNNER,
                '            f"{settings.timeout + KILL_MARGIN:g}",\n',
                '            "86400",  # switched off\n',
            ),
        ),
        stack="up",
        expect=(
            Expect(
                f"{SANDBOX}::test_a_command_past_its_timeout_is_killed_and_its_container_removed",
                "assert done.timed_out",
            ),
            Expect(
                f"{SANDBOX}::test_a_timed_out_command_becomes_a_tool_result_and_the_job_goes_on",
                'assert timed_out["is_error"] is True',
            ),
        ),
    ),
    "halt-leaves-the-call-running": SwitchedOff(
        issue=158,
        what="the cancel of a running sandboxed command when its call ends early, as on a halt",
        edits=(
            Edit(
                SANDBOXED,
                "        if not task.done():\n"
                "            task.cancel()\n"
                "            # Not suppressed: a cancel of this call that arrives while the\n"
                "            # container is removed propagates.\n"
                "            await asyncio.wait({task})\n",
                "        pass  # switched off\n",
            ),
        ),
        stack="up",
        expect=tuple(
            Expect(f"{SANDBOX}::{test}", "assert left == []")
            for test in (
                "test_a_cancel_kills_a_running_command_and_its_container",
                "test_the_deadline_kills_a_running_command_and_fails_the_job",
                "test_a_lost_lease_kills_a_running_command",
            )
        ),
    ),
    # Workspace size
    "workspace-watcher-off": SwitchedOff(
        issue=158,
        what="the measurement of the workspace while a command runs",
        edits=(
            Edit(
                SANDBOXED,
                "watched = await _watched(started, call.workspace, settings, limit)",
                "watched = await started  # switched off",
            ),
        ),
        stack="up",
        expect=(
            Expect(
                f"{WATCHER}::test_a_call_that_writes_past_the_limit_is_stopped",
                "assert result.is_error",
            ),
            Expect(
                f"{WATCHER}::test_a_run_that_ends_does_not_wait_for_a_measurement",
                "assert measured >= 2",
            ),
            Expect(
                f"{SANDBOX}::test_a_command_that_writes_past_the_workspace_limit_is_stopped",
                "assert result.is_error",
            ),
        ),
    ),
    "workspace-precheck-off": SwitchedOff(
        issue=158,
        what="the measurement of the workspace before a command starts",
        edits=(
            Edit(
                SANDBOXED,
                "if limit is not None"
                " and (reason := await _over(call.workspace, settings, limit)):",
                "if limit is not None and (reason := None):  # switched off",
            ),
        ),
        stack="up",
        expect=(
            *(
                Expect(f"{WATCHER}::{test}", "assert result.is_error")
                for test in (
                    "test_a_workspace_over_the_limit_starts_no_command",
                    "test_a_workspace_with_too_many_files_starts_no_command",
                    "test_a_walk_past_its_own_deadline_starts_no_command",
                    "test_a_walk_that_hangs_past_its_time_box_starts_no_command",
                )
            ),
            Expect(
                f"{SANDBOX}::test_a_workspace_over_its_limit_runs_no_command",
                'assert result.content.endswith("its limit of 1.0 MiB. The command did not'
                ' start.")',
            ),
        ),
    ),
    "workspace-entry-cap-off": SwitchedOff(
        issue=158,
        what="the cap on the files and directories one walk counts",
        edits=(
            Edit(
                WORKSPACE,
                "        if self.entries > self.bounds.entries:\n            raise OverLimit(",
                "        if False:  # switched off\n            raise OverLimit(",
            ),
        ),
        stack="none",
        expect=(
            Expect(
                f"{WATCHER}::test_the_walk_stops_at_the_entry_limit",
                'pytest.raises(OverLimit, match="its limit of 20 files and directories")',
            ),
            Expect(
                f"{WATCHER}::test_a_workspace_with_too_many_files_starts_no_command",
                "assert result.is_error",
            ),
        ),
    ),
    "workspace-walk-deadline-off": SwitchedOff(
        issue=158,
        what="the deadline of one walk of the workspace",
        edits=(
            Edit(
                WORKSPACE,
                "            if time.monotonic() > self.bounds.deadline:",
                "            if False:  # switched off",
            ),
        ),
        stack="none",
        expect=(
            Expect(
                f"{WATCHER}::test_a_walk_past_its_deadline_cannot_be_measured",
                'pytest.raises(Unmeasurable, match="took too long")',
            ),
            Expect(
                f"{WATCHER}::test_a_walk_past_its_own_deadline_starts_no_command",
                "assert result.is_error",
            ),
        ),
    ),
    "workspace-run-bound-off": SwitchedOff(
        issue=158,
        what="the part of a call's bound kept for the measurement",
        edits=(
            Edit(
                RUNNER,
                "within = call_within(settings) - measure_within(settings)",
                "within = call_within(settings)  # switched off",
            ),
        ),
        stack="none",
        expect=(
            Expect(
                "tests/unit/test_sandbox_runner.py"
                "::test_with_a_workspace_limit_the_run_leaves_the_measurement_its_part_of_the_bound",
                "assert took < run_bound + 0.3",
            ),
        ),
    ),
    "workspace-timeout-off": SwitchedOff(
        issue=158,
        what="the time box of one measurement of the workspace",
        edits=(Edit(SANDBOXED, "async with asyncio.timeout(within):", "if True:  # switched off"),),
        stack="none",
        expect=(
            Expect(
                f"{WATCHER}::test_a_walk_that_hangs_past_its_time_box_starts_no_command",
                "assert result.is_error",
            ),
        ),
    ),
    "workspace-stop-signal-off": SwitchedOff(
        issue=158,
        what="the signal that tells a walk to stop past its time box or on a cancel",
        edits=(Edit(SANDBOXED, "        bounds.stop.set()\n", "        pass  # switched off\n"),),
        stack="none",
        expect=(
            Expect(
                f"{WATCHER}::test_a_walk_that_hangs_past_its_time_box_starts_no_command",
                "assert took < 1.0",
            ),
            Expect(
                f"{WATCHER}::test_a_run_that_ends_does_not_wait_for_a_measurement",
                "assert stopped[-1] is True",
            ),
        ),
    ),
    "workspace-walk-stop-off": SwitchedOff(
        issue=158,
        what="a walk's check of the signal to stop",
        edits=(
            Edit(
                WORKSPACE,
                "            if self.bounds.stop.is_set():",
                "            if False:  # switched off",
            ),
        ),
        stack="none",
        expect=(
            Expect(
                f"{WATCHER}::test_a_walk_told_to_stop_cannot_be_measured",
                'pytest.raises(Unmeasurable, match="was stopped")',
            ),
        ),
    ),
}


def _once(edit: Edit, text: str) -> str:
    found = text.count(edit.old)
    if found != 1:
        raise ValueError(f"{edit.path}: the edit's text occurs {found} times, not once")
    return text.replace(edit.old, edit.new)


def edited(edit: Edit, root: Path = ROOT) -> str:
    """The file's text with the edit made. Raises ValueError unless `old` occurs once."""
    return _once(edit, (root / edit.path).read_text())


def edited_files(edits: tuple[Edit, ...], root: Path = ROOT) -> dict[str, str]:
    """Each file the edits change, with all of its edits made, in the list's order.

    Two edits of one file both land: each must occur once in the text the
    edits before it left. Raises ValueError otherwise. A path is relative to
    `root`, under `src/`, `migrations/` or anywhere else in the checkout.
    """
    texts: dict[str, str] = {}
    for edit in edits:
        text = texts[edit.path] if edit.path in texts else (root / edit.path).read_text()
        texts[edit.path] = _once(edit, text)
    return texts


def stack_commands(stack: str, provider: str) -> list[list[str]]:
    """The commands that start an entry's stack, before its tests run. None with no stack."""
    match stack:
        case "none":
            return []
        case "up":
            return [["make", "up"]]
        case "chaos":
            compose = ["-f", "docker-compose.yml", "-f", "docker-compose.chaos.yml"]
            return [["docker", "compose", *compose, "up", "--build", "--wait"]]
        case "kills":
            compose = ["-f", "docker-compose.yml", "-f", "docker-compose.kills.yml"]
            if provider == "claude":
                # The one file that names the Claude API. It reads the key.
                compose += ["-f", "docker-compose.claude.yml"]
            return [["docker", "compose", *compose, "up", "--build", "--wait"]]
        case "lab":
            # As `make lab`: the lab's tests start the stack with Gitea from
            # these images, and put the stack of `make up` back at the end.
            compose = ["-f", "docker-compose.yml", "-f", "docker-compose.lab.yml"]
            return [["docker", "compose", *compose, "build"]]
    raise ValueError(f"no stack {stack!r}")


def kill_rules(root: Path = ROOT) -> ModuleType:
    """`tests/kills/rules.py` of `root`, loaded by its path. It imports nothing of the stack."""
    name = "switched_off_kill_rules"
    spec = importlib.util.spec_from_file_location(name, root / KILL_RULES)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"{KILL_RULES} cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    # Registered before it runs: the dataclasses in it look their module up.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def options(workflow: str, field: str) -> list[str]:
    """The options of one input of the workflow, read from its text: `[a, b]` or a `- a` list."""
    lines = workflow.splitlines()
    start = lines.index(f"      {field}:")
    at = next(i for i in range(start, len(lines)) if lines[i].strip().startswith("options:"))
    inline = lines[at].split("options:", 1)[1].strip()
    if inline:
        return [name.strip() for name in inline.strip("[]").split(",")]
    names = []
    for line in lines[at + 1 :]:
        if not line.strip().startswith("- "):
            break
        names.append(line.strip()[2:])
    return names


def check(root: Path = ROOT) -> list[str]:
    """What is wrong with the list against the code in `root`. Empty when nothing is."""
    problems = []
    for name, entry in ENTRIES.items():
        try:
            for path, text in edited_files(entry.edits, root).items():
                compile(text, path, "exec")
        except (ValueError, SyntaxError) as error:
            problems.append(f"{name}: {error}")
        for expect in entry.expect:
            path, _, function = expect.node.partition("::")
            function = function.partition("[")[0]
            test_file = root / path
            if not test_file.is_file() or f"def {function}(" not in test_file.read_text():
                problems.append(f"{name}: no test {expect.node}")
            elif expect.fails_with is not None and expect.fails_with not in test_file.read_text():
                problems.append(f"{name}: {path} has no {expect.fails_with!r}")
        problems += [f"{name}: {problem}" for problem in run_problems(entry, root)]
    return problems


def run_problems(entry: SwitchedOff, root: Path = ROOT) -> list[str]:
    """What is wrong with an entry's provider, stack, jobs and record. Empty when nothing is.

    An entry on the Claude API gets the key. So its model is one the
    workflow offers, its stack is the kill runs', its jobs times the budget
    of each stay inside the bound, and it is judged by its record.
    """
    problems = []
    kills = entry.stack == "kills"
    if entry.provider == "claude":
        offered = options((root / WORKFLOW).read_text(), "model")
        if entry.model not in offered:
            problems.append(f"the workflow offers no model {entry.model!r}")
        if not kills:
            problems.append(f"a run on the Claude API uses the kills stack, not {entry.stack!r}")
        if entry.record is None:
            problems.append("a run on the Claude API is judged by its record, and names none")
    elif entry.model is not None:
        problems.append(f"the fake provider takes no model, and the entry names {entry.model!r}")
    if entry.stack in STACK_TESTS:
        tests = STACK_TESTS[entry.stack]
        outside = [expect.node for expect in entry.expect if not expect.node.startswith(tests)]
        if outside:
            problems.append(f"stack {entry.stack!r} runs {tests} only, not {outside}")
    if not kills:
        if entry.runs or entry.record is not None:
            problems.append("`runs` and `record` are for the kills stack")
        return problems
    rules = kill_rules(root)
    if not 1 <= entry.runs <= rules.API_JOB_LIMIT:
        problems.append(f"`runs` is {entry.runs}, not 1 to {rules.API_JOB_LIMIT}")
    spent = entry.runs * rules.TOKEN_BUDGET
    if entry.provider == "claude" and spent > KEYED_TOKEN_BOUND:
        problems.append(f"{entry.runs} jobs may spend {spent} tokens, over {KEYED_TOKEN_BOUND}")
    outside = [expect.node for expect in entry.expect if not expect.node.startswith("tests/kills/")]
    if outside:
        problems.append(f"the kills stack runs tests/kills only, not {outside}")
    if entry.record is None:
        return problems
    missing = sorted({SENT, ANSWERED, REFUSED} - set(rules.thinking_record([])))
    if missing:
        problems.append(f"{KILL_RULES} records no {missing}")
    tests = (root / KILL_RUNS).read_text()
    problems += [f"{KILL_RUNS} records no {key}" for key in JUDGED[3:] if f'"{key}"' not in tests]
    # The record of a failpoint test is named after its failpoint.
    named = [f for f in rules.FAILPOINTS if entry.record.test == f"failpoint {f}"]
    nodes = [expect.node for expect in entry.expect]
    if not named or len(nodes) != 1 or not nodes[0].endswith(f"[{named[0]}]"):
        problems.append(f"the record {entry.record.test!r} is not the one test's: {nodes}")
    if not 1 <= entry.record.at_least <= entry.runs:
        problems.append(f"`at_least` is {entry.record.at_least}, not 1 to {entry.runs}")
    return problems


def outcomes(junit: Path) -> dict[str, tuple[str, str]]:
    """Each test case in a junit file: node id -> (passed | failed | error | skipped, text).

    A test that failed and whose teardown failed too has two cases. The
    later one counts: an error.
    """
    found: dict[str, tuple[str, str]] = {}
    for case in ET.parse(junit).getroot().iter("testcase"):  # noqa: S314 - pytest wrote it
        path = case.get("classname", "").replace(".", "/") + ".py"
        node = f"{path}::{case.get('name', '')}"
        result, text = "passed", ""
        for kind in ("failure", "error", "skipped"):
            element = case.find(kind)
            if element is not None:
                result = "failed" if kind == "failure" else kind
                text = _failing_lines(element.get("message", ""), element.text or "")
                break
        if found.get(node, ("passed", ""))[0] == "error":
            continue
        found[node] = (result, text)
    return found


def _failing_lines(message: str, traceback: str) -> str:
    """The failure's message and the statements that failed, nothing else.

    pytest's traceback shows the test's source up to the failing line, so the
    named assertion can appear in it as context while another one failed.
    pytest marks each failing statement with '>' at the start of its line.
    """
    marked = [line for line in traceback.splitlines() if line.startswith(">")]
    return "\n".join([message, *marked])


def judge(entry: SwitchedOff, found: dict[str, tuple[str, str]]) -> list[str]:
    """One line per expected test; a line that starts with "WRONG" fails the run."""
    lines = []
    for expect in entry.expect:
        result, text = found.get(expect.node, ("not run", ""))
        if expect.fails_with is None:
            ok = result == "passed"
            want = f"passed ({expect.passes_because})"
        else:
            ok = result == "failed" and expect.fails_with in text
            want = f"failed on {expect.fails_with!r}"
        lines.append(f"{'ok' if ok else 'WRONG'}: {expect.node}: {result}, expected {want}")
    return lines


def new_records(
    directory: Path, before: set[str], test: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """The run's own record of `test`, and why there is not exactly one.

    Only a directory the run created is read: `directory` keeps the records
    of earlier runs too. `before` are the names it held when the run started.
    """
    new = [d for d in sorted(directory.glob("*")) if d.is_dir() and d.name not in before]
    if len(new) != 1:
        return [], [f"the run left {len(new)} new directories in {directory.name}, not one"]
    file = new[0] / "results.jsonl"
    if not file.is_file():
        return [], [f"{new[0].name} has no results.jsonl"]
    records = []
    for line in file.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            return [], [f"a line of {new[0].name}/results.jsonl is not JSON"]
        if isinstance(record, dict) and record.get("test") == test:
            records.append(record)
    if len(records) != 1:
        return [], [f"{new[0].name}/results.jsonl has {len(records)} records of {test!r}, not one"]
    return records, []


def _count(record: Mapping[str, Any], key: str) -> int | None:
    value = record.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def not_this_run(
    record: Mapping[str, Any], *, runs: int, provider: str, model: str, head: str
) -> list[str]:
    """Why a record is not the record of this run of the entry. Empty when it is."""
    wrong = []
    for key, wanted in (("runs", runs), ("provider", provider), ("model", model)):
        if record.get(key) != wanted or isinstance(record.get(key), bool):
            wrong.append(f"the record's {key} is {record.get(key)!r}, not {wanted!r}")
    # The runner records the short id, and that the checkout is edited.
    short = str(record.get("commit", "")).split(" ")[0]
    if len(short) < 7 or not head.startswith(short):
        wrong.append(f"the record's commit is {record.get('commit')!r}, not {head[:12]}")
    return wrong


def judge_record(
    entry: SwitchedOff,
    tests: list[str],
    records: list[dict[str, Any]],
    trouble: list[str],
    *,
    head: str,
    rules: ModuleType,
) -> tuple[str, list[str]]:
    """PASS, FINDING or INCONCLUSIVE for a run on the Claude API, and the reasons (ADR 0022).

    `tests` are the lines of `judge`. `records` and `trouble` are what
    `new_records` found. Only PASS is a pass. A finding is recorded, and
    the entry is not run again or changed to make it pass. An
    inconclusive run shows nothing either way.
    """
    if entry.record is None or entry.model is None:
        raise ValueError("only an entry with a record and a model is judged by its record")
    if trouble:
        return "INCONCLUSIVE", [f"no record to judge: {problem}" for problem in trouble]
    record = records[0]
    wrong = not_this_run(
        record, runs=entry.runs, provider="Claude API", model=entry.model, head=head
    )
    counts = {key: _count(record, key) for key in JUDGED}
    wrong += [f"the record has no count {key}" for key, count in counts.items() if count is None]
    if wrong:
        return "INCONCLUSIVE", wrong
    if record.get("verdict") == "void":
        return "INCONCLUSIVE", [f"the run is void: {record.get('void')}"]
    sent, answered, refused = (counts[key] or 0 for key in (SENT, ANSWERED, REFUSED))
    failed, problems = counts["failed_runs"] or 0, counts["problems"] or 0
    errors = [str(error) for error in record.get("errors_of_failed_jobs") or []]
    lines = [
        f"{entry.runs} jobs: {sent} sent a rebuilt thinking block, {refused} of them were"
        f" refused for its signature, {answered} were answered; {failed} jobs failed"
        f" with {problems} problems",
        *(f"a failed job's error: {error}" for error in errors),
    ]
    if answered:
        return "FINDING", [*lines, f"the API answered {answered} jobs that sent a changed block"]
    if failed != refused or problems != refused or sent != refused:
        if any(rules.OTHER_CONVERSATION in error for error in errors):
            lines.append(
                f'the API says a block is "{rules.OTHER_CONVERSATION}": replay changed'
                " the conversation, not only the signature"
            )
        return "FINDING", [*lines, "a job failed another way than by the API's refusal"]
    if refused < entry.record.at_least:
        few = f"{refused} jobs sent a changed block, fewer than {entry.record.at_least}"
        return "INCONCLUSIVE", [*lines, few]
    if record.get("verdict") != "failed" or not all(line.startswith("ok") for line in tests):
        return "FINDING", [
            *lines,
            f"the test did not end as the list says; its record's verdict is"
            f" {record.get('verdict')!r}",
        ]
    return "PASS", [
        *lines,
        "every job that sent a changed block was refused, and every other job passed",
    ]


def judge_rehearsal(
    entry: SwitchedOff,
    found: dict[str, tuple[str, str]],
    records: list[dict[str, Any]],
    trouble: list[str],
    *,
    head: str,
    rules: ModuleType,
) -> tuple[str, list[str]]:
    """REHEARSAL OK or REHEARSAL WRONG for an entry of the Claude API run on the fake provider.

    The fake provider sends no `thinking` block, so the edit changes
    nothing. The one outcome that is right: the test passes, and the run's
    one record says no job sent a rebuilt block and none failed. Anything
    else is a fault in the run's steps, found before a run that costs money.
    """
    wrong = list(trouble)
    for expect in entry.expect:
        result, _ = found.get(expect.node, ("not run", ""))
        if result != "passed":
            wrong.append(f"{expect.node}: {result}, expected passed")
    if not trouble:
        record = records[0]
        wrong += not_this_run(
            record, runs=entry.runs, provider="fake provider", model=rules.FAKE_MODEL, head=head
        )
        if record.get("verdict") != "passed":
            void = f" ({record['void']})" if "void" in record else ""
            wrong.append(f"the record's verdict is {record.get('verdict')!r}{void}, not 'passed'")
        wrong += [
            f"the record's {key} is {record.get(key)!r}, not 0"
            for key in JUDGED
            if _count(record, key) != 0
        ]
    if wrong:
        return "REHEARSAL WRONG", wrong
    return "REHEARSAL OK", [
        f"{entry.runs} jobs on the fake provider: none sent a rebuilt thinking block,"
        " all passed. Not a result."
    ]


def names(provider: str) -> list[str]:
    """The entries of one provider, in the list's order."""
    return [name for name, entry in ENTRIES.items() if entry.provider == provider]


def dispatch_problem(name: str, provider: str, model: str | None) -> str | None:
    """Why the workflow's inputs do not start this entry, or None.

    The entry names its provider and model. The inputs have to say the
    same, so the job that runs and its name are the entry's.
    """
    entry = ENTRIES[name]
    if provider != entry.provider:
        return f"{name} runs on provider {entry.provider}, not {provider}"
    if entry.provider == "claude" and model != entry.model:
        return f"{name} runs on model {entry.model}, not {model}"
    return None


def start_problem(name: str, asked: str, *, rehearsal: bool) -> str | None:
    """Why this start is refused, or None. `asked` is SWITCHED_OFF_PROVIDER, `fake` if unset."""
    entry = ENTRIES[name]
    if rehearsal:
        if entry.provider != "claude":
            return f"{name} runs on the fake provider as it is: nothing to rehearse"
        if asked != "fake":
            return f"a rehearsal runs on the fake provider: unset {PROVIDER_VARIABLE}"
        return None
    if entry.provider == "claude" and asked != "claude":
        return (
            f"{name} runs on the Claude API and every job costs money: set"
            f" {PROVIDER_VARIABLE}=claude, or rehearse it on the fake provider"
        )
    if entry.provider == "fake" and asked != "fake":
        return f"{name} runs on the fake provider: unset {PROVIDER_VARIABLE}"
    return None


def without_keys(env: Mapping[str, str]) -> dict[str, str]:
    return {name: value for name, value in env.items() if name not in KEY_VARIABLES}


def child_environment(entry: SwitchedOff, provider: str, env: Mapping[str, str]) -> dict[str, str]:
    """The environment of the stack and the tests of a run, made from the entry.

    No inherited `KILLS_*` variable and no failpoint reaches them: the entry
    alone names the provider, the model and the number of jobs. The key is
    passed on only to a run on the Claude API.
    """
    dropped = ("FAILPOINT", PROVIDER_VARIABLE)
    kept = {
        name: value
        for name, value in without_keys(env).items()
        if not name.startswith("KILLS_") and name not in dropped
    }
    if provider == "claude" and "CLAUDE_API_KEY" in env:
        kept["CLAUDE_API_KEY"] = env["CLAUDE_API_KEY"]
    if entry.stack == "kills":
        kept["KILLS_PROVIDER"] = provider
        if provider == "claude" and entry.model is not None:
            kept["KILLS_MODEL"] = entry.model
        # Whichever test of the kill runs the entry names runs this many
        # jobs, and the runner refuses one more.
        for name in ("KILLS_RUNS", "KILLS_FAILPOINT_RUNS", "KILLS_JOB_LIMIT"):
            kept[name] = str(entry.runs)
    return kept


def _sh(*args: str, env: Mapping[str, str]) -> None:
    print("+", " ".join(args), flush=True)
    subprocess.run(args, cwd=ROOT, check=True, env=dict(env))  # noqa: S603 - fixed commands


def _git(*args: str) -> str:
    done = subprocess.run(  # noqa: S603 - fixed commands
        ["git", *args],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        env=without_keys(os.environ),
    )
    return done.stdout


def run(name: str, *, rehearsal: bool = False) -> int:
    entry = ENTRIES[name]
    refused = start_problem(name, os.environ.get(PROVIDER_VARIABLE, "fake"), rehearsal=rehearsal)
    if refused is not None:
        print(f"not started: {refused}", file=sys.stderr)
        return 2
    # CI checks the list on every PR. A run checks its own entry again.
    wrong = run_problems(entry)
    if wrong:
        print(f"not started: {'; '.join(wrong)}", file=sys.stderr)
        return 2
    provider = "fake" if rehearsal else entry.provider
    if rehearsal:
        print(f"rehearsal of {name} on the fake provider: not a result", flush=True)
    print(f"switched off: {name} (issue {entry.issue}): {entry.what}", flush=True)
    changed = _git("status", "--porcelain", "--untracked-files=no")
    if changed:
        print(f"WRONG: the checkout has changes; the run would not be HEAD's code:\n{changed}")
        return 1
    head = _git("rev-parse", "HEAD").strip()
    print(f"commit {head}", flush=True)
    env = child_environment(entry, provider, os.environ)
    quiet = without_keys(env)
    starts = stack_commands(entry.stack, provider)
    texts = edited_files(entry.edits)
    for path, text in texts.items():
        (ROOT / path).write_text(text)
    _sh("git", "--no-pager", "diff", "--", *texts, env=quiet)
    if entry.stack == "chaos":
        (ROOT / ".chaos").mkdir(exist_ok=True)
        # The chaos workers write their records here, as with `make chaos`.
        (ROOT / ".chaos").chmod(0o777)
    for command in starts:
        _sh(*command, env=env)
    (ROOT / JUNIT).parent.mkdir(exist_ok=True)
    (ROOT / JUNIT).unlink(missing_ok=True)
    kept = ROOT / KILL_RECORDS
    before = {found.name for found in kept.glob("*")}
    nodes = [expect.node for expect in entry.expect]
    pytest = ["uv", "run", "--frozen", "pytest", "-v", "-s", "-p", "no:cacheprovider"]
    command = [*pytest, f"--junitxml={JUNIT}", *nodes]
    # A fixed command; the node ids come from the list above.
    tests = subprocess.run(command, cwd=ROOT, check=False, env=env)  # noqa: S603
    print(f"pytest exit {tests.returncode}", flush=True)
    found = outcomes(ROOT / JUNIT) if (ROOT / JUNIT).is_file() else {}
    lines = judge(entry, found) if found else ["WRONG: pytest wrote no results"]
    if entry.record is None:
        print("\n".join(lines), flush=True)
        return 1 if any(line.startswith("WRONG") for line in lines) else 0
    records, trouble = new_records(kept, before, entry.record.test)
    rules = kill_rules()
    if rehearsal:
        verdict, reasons = judge_rehearsal(entry, found, records, trouble, head=head, rules=rules)
    else:
        print("\n".join(lines), flush=True)
        verdict, reasons = judge_record(entry, lines, records, trouble, head=head, rules=rules)
    print("\n".join(reasons), flush=True)
    print(f"{verdict}: {name}", flush=True)
    return 0 if verdict in ("PASS", "REHEARSAL OK") else 1


def main(argv: list[str]) -> int:
    match argv:
        case ["names"]:
            print("\n".join(ENTRIES))
            return 0
        case ["names", "fake"]:
            print(json.dumps(names("fake")))
            return 0
        case ["check"]:
            problems = check()
            print("\n".join(problems) or "every edit matches once and compiles")
            return 1 if problems else 0
        case ["dispatch", name, provider] if name in ENTRIES:
            return _dispatch(name, provider, None)
        case ["dispatch", name, provider, model] if name in ENTRIES:
            return _dispatch(name, provider, model)
        case ["run", name] if name in ENTRIES:
            return run(name)
        case ["rehearse", name] if name in ENTRIES:
            return run(name, rehearsal=True)
        case ["run" | "rehearse" | "dispatch", _, *_]:
            print(f"not a switched-off run; one of: {', '.join(ENTRIES)}", file=sys.stderr)
            return 2
    print(__doc__, file=sys.stderr)
    return 2


def _dispatch(name: str, provider: str, model: str | None) -> int:
    problem = dispatch_problem(name, provider, model)
    if problem is None and (wrong := run_problems(ENTRIES[name])):
        problem = "; ".join(wrong)
    if problem is not None:
        print(f"not started: {problem}", file=sys.stderr)
        return 1
    print(f"{name}: provider {provider}" + (f", model {model}" if provider == "claude" else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
