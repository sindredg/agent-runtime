"""Switched-off runs: remove one mechanism, run the tests that guard it, expect them to fail.

A guarantee is "tested" only with a run that removes its mechanism and fails
its test (README). Each entry below names the edit, the tests, and how each
test must end: failed on a named assertion, or passed for a stated reason.
The list is fixed before a run. A test that ends otherwise fails the run, and
is a finding for the results page, not a reason to change the entry.

The Manual runs workflow runs one entry, or every entry of the fake provider
with one job each, on `main` (ADR 0021). An entry names its provider. One on
the Claude API runs in a job that holds the key, and is judged by a record
as well. On the kills stack that is the kill runner's record (ADR 0022). On
the long stack it is the long-job harness's: two graded jobs, a control on
the checkout as it is and, only after it passed, one with the edit made.
Such an entry starts only while the token bound of keyed long jobs is set
and its jobs fit it.

    python scripts/switched_off.py names          the entries, one per line
    python scripts/switched_off.py names fake     the entries of the fake provider, as a
                                                  JSON list: the workflow's matrix
    python scripts/switched_off.py check          the list still fits the code
    python scripts/switched_off.py dispatch NAME PROVIDER [MODEL [JOB]]
                                                  refuse a start that is not the entry's;
                                                  JOB is `long-job` for an entry of the
                                                  long-job harness, and only for one
    python scripts/switched_off.py run NAME       edit the checkout, start the stack, run, judge
    python scripts/switched_off.py rehearse NAME  an entry of the Claude API on the fake
                                                  provider: every step of `run`, and no
                                                  result. On the long stack: the control
                                                  job only, with no edit

`run` and `rehearse` change files in the checkout. Run them on a clean
checkout and restore it with `git checkout -- .` afterwards.

`run` starts an entry of the Claude API only with SWITCHED_OFF_PROVIDER=claude.
Every job then costs money.
"""

import importlib.util
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any, Literal

from runtime.logs import text_safe

ROOT = Path(__file__).resolve().parents[1]
JUNIT = Path(".switched-off/junit.xml")
WORKFLOW = ".github/workflows/manual-runs.yml"
AGENT = "src/runtime/worker/agent.py"
SANDBOXED = "src/runtime/tools/sandboxed.py"
WORKSPACE = "src/runtime/sandbox/workspace.py"
RUNNER = "src/runtime/sandbox/runner.py"
CREATE_ISSUE = "src/runtime/tools/create_issue.py"
CHAIN_MIGRATION = "migrations/versions/0007_step_log_chain.py"
KILLS = "tests/chaos/test_kills.py"
FAILPOINT = f"{KILLS}::test_a_job_survives_its_worker_stopping_at_the_failpoint"
HALT = "tests/integration/test_halt_store.py"
SANDBOX = "tests/integration/test_sandbox.py"
STALLED = "tests/integration/test_stalled_tool_call.py"
CREATE_ISSUE_TESTS = "tests/unit/test_create_issue.py"
WATCHER = "tests/unit/test_workspace_watcher.py"
APPROVAL = "tests/integration/test_approval.py"
CHAOS_NOTIFY = "tests/chaos/test_notify.py"
CHAOS_APPROVAL = "tests/chaos/test_approval.py"
REPEAT_STOP = "tests/integration/test_repeat_stop.py"
BACKOFF_WORKER = "tests/integration/test_backoff_worker.py"
BUSY = f"{BACKOFF_WORKER}::test_a_busy_provider_makes_the_job_wait_and_then_finish"
BUSY_CASES = ("[429-rate_limit_error]", "[529-overloaded_error]")
IN_STACK = "tests/integration/test_backoff_in_stack.py::test_a_rate_limited_job_waits_and_succeeds"
PROVIDER_ERRORS = "tests/integration/test_provider_errors.py"
MAY_PASS = f"{PROVIDER_ERRORS}::test_an_error_that_may_pass_makes_the_job_wait_and_then_finish"
MAY_PASS_CASES = (
    "[status-408]",
    "[status-409]",
    "[status-500]",
    "[status-502]",
    "[status-503]",
    "[status-504]",
    "[stream-api-error]",
    "[stream-timeout-error]",
    "[stream-overloaded]",
    "[stream-rate-limit]",
    "[broken-stream]",
    "[no-stop-reason]",
    "[not-a-stream]",
    "[client-timeout]",
)
ELEVENTH = f"{PROVIDER_ERRORS}::test_the_eleventh_error_in_a_row_fails_the_job_and_names_the_cap"
JOB_CAP = f"{PROVIDER_ERRORS}::test_the_job_cap_counts_errors_across_responses"
BUSY_BETWEEN = f"{PROVIDER_ERRORS}::test_a_busy_answer_between_errors_does_not_reset_the_count"
ERROR_TAKEOVER = f"{PROVIDER_ERRORS}::test_the_counts_survive_a_takeover_by_another_worker"
STREAM_CAP = f"{PROVIDER_ERRORS}::test_a_busy_error_inside_a_stream_counts_toward_the_cap"
STREAM_CAP_CASES = ("[overloaded_error]", "[rate_limit_error]")
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
# The long-job suite: its tests, and the issue its switched-off runs are recorded under.
LONG = "tests/long/test_long_job.py"
LONG_UNINTERRUPTED = f"{LONG}::test_a_job_longer_than_the_window_finishes_through_three_summaries"
LONG_RUNS_ISSUE = 237
# Record-shaped text in a tool result or a summary: its tests, and the issue.
RECORD_SHAPED = "tests/unit/test_record_shaped_text.py"
RECORD_SHAPED_STACK = "tests/integration/test_record_shaped_text_in_stack.py"
RECORD_SHAPED_ISSUE = 160
# One tool call a response of a job with a trigger: its tests, and the issue.
ONE_CALL_TESTS = "tests/unit/test_summary.py"
ONE_CALL_ISSUE = 258
# The long-job harness: its tests, its rules and grader, where a session leaves its
# records, the record's test, and the issue of its entry.
LONG_JOB = "tests/long_job/test_long_job_run.py"
LONG_JOB_TESTS = "tests/long_job/"
LONG_JOB_RULES = "tests/long_job/rules.py"
LONG_JOB_GRADER = "tests/long_job/grader.py"
LONG_JOB_RECORDS = ".long-job"
LONG_JOB_TEST = "long-job"
LONG_JOB_ISSUE = 243
# Run in each phase of such an entry beside its tests: the session's answers
# in what it printed, recorded and kept. It is no test of the entry's.
LONG_JOB_ANSWERS = f"{LONG_JOB}::test_no_answer_is_in_the_record_the_output_or_the_saved_logs"
# The message of that test's assert on what it found: its one failure that is a leak.
ANSWER_FOUND = "the record, the output or a saved worker log holds an answer of the job"
# How pytest runs a phase: short tracebacks, and output captured for the
# answers test. A failure then prints its message and the count, no more.
LONG_JOB_PYTEST = ("-v", "--tb=short")
# The tests that a run with no stack, the lab's stack and the long stack may
# name. On the Claude API the long stack runs the long-job harness instead.
STACK_TESTS = {"none": "tests/unit/", "lab": "tests/lab/", "long": "tests/long/"}
# What an edit may change: the product's code and its migrations, never a test,
# the workflow or this script.
EDITABLE = ("src/", "migrations/")
# An entry's name reaches the workflow's options, a job's name and the shell.
ENTRY_NAME = re.compile(r"[a-z0-9-]+")
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
# What the long stack reads from the shell: the workers' trigger and summary
# failpoint limit, the fake provider's scenarios and the tracker's first number.
LONG_VARIABLES = (
    "SUMMARY_TRIGGER",
    "LONG_SCENARIO_DIR",
    "FAILPOINT_SUMMARY",
    "FAKE_TRACKER_FIRST_ISSUE",
)
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
class Verdicts:
    """What the two graded jobs of an entry of the long-job harness must show.

    The control job runs the checkout as it is: its record's verdict is
    `control` and every test of the entry passes. Only then is the edit
    written. The edited job runs to its end: its record lists each clause
    of `fires`, meets every floor, and has neither R1 nor NO_CLASS.
    """

    control: str = "PASS"
    fires: tuple[str, ...] = ("R7", "R8", "R9")


@dataclass(frozen=True)
class SwitchedOff:
    issue: int
    what: str
    edits: tuple[Edit, ...]
    # `none`: no stack, for unit tests. `up`: the normal stack. `chaos`: three
    # workers and a short lease. `kills`: the kill runs' stack, three workers
    # and a 6 s lease. `lab`: the images built, and the lab's tests start the
    # stack with Gitea themselves. `long`: the stack of `make long-up`, for a
    # job longer than the context window; on the Claude API with its overlay too.
    stack: Literal["none", "up", "chaos", "kills", "lab", "long"]
    expect: tuple[Expect, ...]
    # `claude`: the run is on the Claude API, in the job that holds the key.
    provider: Literal["fake", "claude"] = "fake"
    # The model of a run on the Claude API.
    model: str | None = None
    # On the Claude API: the jobs of the run. On the kills stack the most the
    # test may submit; on the long stack the control and the edited job.
    runs: int = 0
    record: Refusals | Verdicts | None = None


# The first summary's text when its notices are dropped: the task and the rules
# of the prompt, and a line that starts the reading again. Equal to
# `NOTICES_DROPPED` of tests/long_job/rules.py; a unit test keeps the two equal.
NOTICES_DROPPED = (
    "Review the access records. Read index.txt, then read every file it lists, in the order"
    " listed. Open one issue for each grant that is a violation.\n"
    "\n"
    "Rules:\n"
    "1. A grant is a violation when its holder has left. A person has left when a leaver"
    " notice names them, in any file, or when they are on a roster of their team and missing"
    " from a later roster of that team.\n"
    '2. A grant on a system marked "approval required" is a violation unless an approval'
    " names its holder and that system. An approval is in the file of its grant.\n"
    "3. Nothing else is a violation.\n"
    "\n"
    "Nothing is recorded as read. Continue through every file that index.txt lists, starting"
    " with the first."
)
SUMMARY_MESSAGE = (
    '                payload["message"] = opening_message(summary_text(response.content))\n'
)
# The job's first summary is stored as that text, and its message is built from
# it: replay rebuilds the message from the stored text and compares the two.
NOTICES_DROPPED_SUMMARY = (
    "                if summaries == 0:  # switched off\n"
    f'                    payload["content"] = [{{"type": "text", "text": {NOTICES_DROPPED!r}}}]\n'
    '                payload["message"] = opening_message(summary_text(payload["content"]))\n'
)

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

# Every run of G12 ends on the question that never reached the receiver.
NOTIFY_EXPECT = tuple(
    Expect(f"{CHAOS_NOTIFY}::{test}", "assert missing == []")
    for test in (
        "test_every_question_reaches_the_receiver_exactly_once_when_nothing_fails",
        "test_every_question_reaches_the_receiver_when_workers_are_killed",
        "test_every_question_reaches_the_receiver_after_it_was_down",
    )
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
                "            return JobStatus.QUEUED, None\n",
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
                "TRUNCATE steps CASCADE",
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
    "repeat-stop-off": SwitchedOff(
        issue=168,
        what="the stop of a job that repeats one call with the same result (ADR 0031)",
        edits=(
            Edit(
                AGENT,
                "            if (stuck := state.stuck_on) is not None:\n",
                "            if False:  # switched off\n",
            ),
        ),
        stack="up",
        expect=(
            Expect(
                f"{REPEAT_STOP}::test_a_job_stuck_on_one_call_fails_after_five_identical_results",
                "assert len(calls) == MAX_REPEATS",
            ),
            Expect(
                f"{REPEAT_STOP}"
                "::test_a_worker_that_takes_over_after_four_results_stops_the_job_after_the_fifth",
                "assert len(calls) == MAX_REPEATS",
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
                # A busy answer is retryable too, so it has to fail here, or
                # the wait on a provider error would take it.
                "                if error.busy:\n"
                '                    raise _JobFailed(f"model call failed: {error}") from error\n'
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
        stack="none",
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
    # A provider error that may pass
    "provider-error-fails": SwitchedOff(
        issue=99,
        what="the wait on a provider error that may pass: it fails the job",
        edits=(
            Edit(
                AGENT,
                "                if error.retryable:\n",
                "                if False:  # switched off\n",
            ),
        ),
        stack="up",
        expect=(
            *(
                Expect(f"{MAY_PASS}{case}", "assert first is JobStatus.WAITING")
                for case in MAY_PASS_CASES
            ),
            Expect(
                f"{PROVIDER_ERRORS}::test_a_retry_after_on_an_error_sets_the_floor_of_the_wait",
                "assert await run_once(engine, provider, stack) is W",
            ),
            Expect(
                f"{PROVIDER_ERRORS}::test_ten_errors_in_a_row_are_waited_out_and_the_job_succeeds",
                "assert statuses == [W] * MAX_ERRORS_IN_A_ROW + [S]",
            ),
            Expect(ELEVENTH, "assert statuses == [JobStatus.WAITING] * 10 + [JobStatus.FAILED]"),
            Expect(JOB_CAP, "assert statuses == [JobStatus.WAITING] * 30 + [JobStatus.FAILED]"),
            Expect(BUSY_BETWEEN, "assert job is not None"),
            Expect(
                ERROR_TAKEOVER,
                'assert await claim_job(conn, worker_id="stopped-worker") is not None',
            ),
            Expect(
                f"{PROVIDER_ERRORS}::test_a_job_whose_cancel_is_requested_does_not_wait_on_an_error",
                "assert status is JobStatus.CANCELLED",
            ),
        ),
    ),
    "provider-error-cap-off": SwitchedOff(
        issue=99,
        what="the caps on waits for a provider error, in a row and in the job",
        edits=(
            Edit(
                AGENT,
                "                    if cap is not None:\n",
                "                    if False:  # switched off\n",
            ),
        ),
        stack="up",
        expect=(
            Expect(ELEVENTH, "assert statuses == [JobStatus.WAITING] * 10 + [JobStatus.FAILED]"),
            Expect(JOB_CAP, "assert statuses == [JobStatus.WAITING] * 30 + [JobStatus.FAILED]"),
            Expect(
                BUSY_BETWEEN, "assert statuses == [JobStatus.WAITING] * 11 + [JobStatus.FAILED]"
            ),
            Expect(ERROR_TAKEOVER, "assert after == [JobStatus.WAITING, JobStatus.FAILED]"),
            *(
                Expect(
                    f"{STREAM_CAP}{case}",
                    "assert statuses == [JobStatus.WAITING] * 10 + [JobStatus.FAILED]",
                )
                for case in STREAM_CAP_CASES
            ),
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
                "            await asyncio.wait({task})\n"
                "        if not task.cancelled():\n",
                # The cancel of the call propagates as it does without the switch;
                # only the run, and with it the container, is left going.
                "        if task.done() and not task.cancelled():  # switched off\n",
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
        stack="none",
        expect=tuple(
            Expect(f"{WATCHER}::{test}", "assert result.is_error")
            for test in (
                "test_a_workspace_over_the_limit_starts_no_command",
                "test_a_workspace_with_too_many_files_starts_no_command",
                "test_a_walk_past_its_own_deadline_starts_no_command",
                "test_a_walk_that_hangs_past_its_time_box_starts_no_command",
            )
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
    # A request to the tracker that outlives its worker's lease (ADR 0025)
    "takeover-wait-for-a-request-off": SwitchedOff(
        issue=103,
        what="the bound create_issue names for its request, which a takeover waits out",
        edits=(
            Edit(
                CREATE_ISSUE,
                "        ends_within=create_within + MARGIN,\n",
                "        # switched off\n",
            ),
        ),
        stack="up",
        expect=(
            Expect(
                f"{STALLED}::test_a_request_that_arrives_after_the_takeover_is_found_and_not_sent_again",
                "assert len(tracker.issues) == 1",
            ),
            Expect(
                f"{STALLED}::test_a_request_given_up_at_its_deadline_is_sent_again_once",
                "assert waited >= 1.0 + MARGIN",
            ),
        ),
    ),
    "create-deadline-off": SwitchedOff(
        issue=103,
        what="the one deadline of the request that opens an issue",
        edits=(
            Edit(
                CREATE_ISSUE,
                "async with asyncio.timeout(within), tracker.sender(transport) as http:",
                "async with tracker.sender(transport) as http:  # switched off",
            ),
        ),
        stack="none",
        expect=(
            Expect(
                f"{CREATE_ISSUE_TESTS}::test_a_request_that_gets_no_answer_is_given_up_at_its_deadline",
                "assert result == ToolResult(",
            ),
            Expect(
                f"{CREATE_ISSUE_TESTS}"
                "::test_an_issue_the_tracker_stored_before_the_deadline_passed_is_reported_as_created",
                "assert slow.given_up",
            ),
        ),
    ),
    "abortive-close-off": SwitchedOff(
        issue=103,
        what="the reset of a given-up request's connection, which drops what was not sent yet",
        edits=(
            Edit(
                "src/runtime/tools/trackers.py",
                "httpx2.AsyncHTTPTransport(socket_options=[ABORTIVE_CLOSE])",
                "httpx2.AsyncHTTPTransport()",
            ),
        ),
        stack="none",
        expect=(
            Expect(
                f"{CREATE_ISSUE_TESTS}::test_giving_a_request_up_resets_its_connection",
                'assert server.ended == ["reset"]',
            ),
        ),
    ),
    # G12: a parked job tells the operator (ADR 0029)
    "notify-loop-off": SwitchedOff(
        issue=75,
        what="the loop in every worker that posts a parked job's question",
        edits=(
            Edit(
                "src/runtime/worker/__main__.py",
                "            if config.notify_url is not None:\n"
                "                tasks.create_task("
                "keep_notifying(engine, config.notify_url, stop))\n",
                "            pass  # switched off\n",
            ),
        ),
        stack="chaos",
        expect=NOTIFY_EXPECT,
    ),
    "notify-row-off": SwitchedOff(
        issue=75,
        what="the notification row the park writes with the job's move",
        edits=(
            Edit(
                "src/runtime/store/held.py",
                "        statement = statement.add_cte(notice)\n",
                "        del notice  # switched off\n",
            ),
        ),
        stack="chaos",
        expect=NOTIFY_EXPECT,
    ),
    "tool-env-passed-on": SwitchedOff(
        issue=169,
        what="the tool container's environment, which holds nothing of the worker's",
        edits=(
            Edit(
                "src/runtime/sandbox/runner.py",
                "import logging\n",
                "import logging\nimport os  # switched off\n",
            ),
            Edit(
                "src/runtime/sandbox/runner.py",
                '        "User": settings.user,\n        "WorkingDir": WORKDIR,\n',
                '        "User": settings.user,\n'
                '        "Env": [f"{name}={os.environ.get(name, \'\')}"'
                ' for name in ("ANTHROPIC_API_KEY", "TRACKER_TOKEN")],  # switched off\n'
                '        "WorkingDir": WORKDIR,\n',
            ),
        ),
        stack="up",
        expect=(
            Expect(
                "tests/integration/test_secret_canary.py"
                "::test_no_secret_reaches_a_step_a_log_line_or_an_answer_of_the_api",
                "a secret was found in:",
            ),
        ),
    ),
    "long-trigger-off": SwitchedOff(
        issue=LONG_RUNS_ISSUE,
        what="the summary trigger: no worker summarises, whatever SUMMARY_TRIGGER says",
        edits=(
            Edit(
                "src/runtime/config.py",
                '            summary_trigger=_optional_positive_int(env, "SUMMARY_TRIGGER"),\n',
                "            summary_trigger=None,  # switched off\n",
            ),
        ),
        stack="long",
        expect=(
            Expect(
                LONG_UNINTERRUPTED,
                "the job's last request was refused for the context window on a scripted turn",
            ),
        ),
    ),
    "long-replay-skips-summary": SwitchedOff(
        issue=LONG_RUNS_ISSUE,
        what="the summary steps in the log a worker reads when it claims a job",
        edits=(
            Edit(
                "src/runtime/worker/replay.py",
                "    for step in steps:\n        state.apply(step)\n",
                "    for step in steps:\n"
                "        if step.kind is not StepKind.SUMMARY:  # switched off\n"
                "            state.apply(step)\n",
            ),
        ),
        stack="long",
        expect=(
            Expect(
                LONG_UNINTERRUPTED,
                None,
                "nobody takes the job over, so no worker reads a log with a summary in it",
            ),
            Expect(
                f"{LONG}::test_a_job_stopped_after_its_first_summary_sends_what_the"
                "_uninterrupted_job_sent",
                "the job stored more summary steps than the three its windows need",
            ),
            Expect(
                f"{LONG}::test_jobs_killed_at_planned_requests_send_what_the"
                "_uninterrupted_job_sent",
                "a request at a key the uninterrupted run lacks",
            ),
        ),
    ),
    "long-summary-cut": SwitchedOff(
        issue=LONG_RUNS_ISSUE,
        what="the whole summary in the message that opens the next window: half of it is cut",
        edits=(
            Edit(
                "src/runtime/worker/summary.py",
                'the summary, a blank line, the continuation."""\n',
                'the summary, a blank line, the continuation."""\n'
                '    text = text[: len(text) // 2] + "\\n\\n" + text.rsplit("\\n", 1)[-1]'
                "  # switched off\n",
            ),
        ),
        stack="long",
        expect=(
            Expect(
                LONG_UNINTERRUPTED,
                "a window does not open with the summary text and the continuation line",
            ),
        ),
    ),
    "long-own-trigger": SwitchedOff(
        issue=LONG_RUNS_ISSUE,
        what="the trigger a job's first request pinned: a worker uses its own setting",
        edits=(
            Edit(
                "src/runtime/worker/replay.py",
                "        return self.pinned_trigger if self.pinned else own\n",
                "        return own  # switched off\n",
            ),
        ),
        stack="long",
        expect=(
            Expect(
                f"{LONG}::test_a_takeover_by_workers_with_another_trigger_sends_what_the"
                "_first_would_have",
                "a worker with another trigger sent other requests than the first worker"
                " would have",
            ),
        ),
    ),
    "long-oversize-off": SwitchedOff(
        issue=LONG_RUNS_ISSUE,
        what="the stop of a job whose window opens at or over the trigger",
        edits=(
            Edit(
                AGENT,
                "            raise _JobFailed(oversize)\n",
                "            pass  # switched off\n",
            ),
        ),
        stack="long",
        expect=(
            Expect(
                f"{LONG}::test_a_window_that_opens_at_the_trigger_fails_the_job_for_it",
                "the window opened at the trigger and the job was not failed for it",
            ),
        ),
    ),
    # The long-job harness on the Claude API: two graded jobs (see `Verdicts`).
    "long-notices-dropped": SwitchedOff(
        issue=LONG_JOB_ISSUE,
        what="the leaver notices and approvals in the job's first summary",
        edits=(Edit(AGENT, SUMMARY_MESSAGE, NOTICES_DROPPED_SUMMARY),),
        stack="long",
        expect=(
            Expect(
                f"{LONG_JOB}::test_every_counted_violation_of_a_notice_has_an_issue",
                "a counted notice-violating grant has no issue",
            ),
            Expect(
                f"{LONG_JOB}::test_no_counted_grant_a_notice_clears_has_an_issue",
                "a counted notice-cleared grant has an issue",
            ),
            Expect(
                f"{LONG_JOB}::test_every_issue_after_the_first_summary_holds_the_reference_code",
                "an issue after the first summary lacks the reference code",
            ),
            Expect(
                f"{LONG_JOB}::test_the_log_holds",
                None,
                "the edit changes what the first summary says, not how the runtime keeps the job",
            ),
            Expect(
                f"{LONG_JOB}::test_the_run_shows_enough",
                None,
                "the files are still read after the first summary; only the notices are lost",
            ),
        ),
        provider="claude",
        model="claude-haiku-5-5",
        runs=2,
        record=Verdicts(),
    ),
    "tool-result-parse": SwitchedOff(
        issue=RECORD_SHAPED_ISSUE,
        what="a tool result as one string in the conversation: replay parses its lines into blocks",
        edits=(
            Edit(
                "src/runtime/worker/replay.py",
                '                    "content": payload["content"],\n',
                '                    "content": [  # switched off\n'
                "                        json.loads(line)\n"
                "                        if line.startswith('{\"type\"')\n"
                '                        else {"type": "text", "text": line}\n'
                '                        for line in payload["content"].splitlines()\n'
                "                    ],\n",
            ),
        ),
        stack="none",
        expect=tuple(
            Expect(
                f"{RECORD_SHAPED}::test_a_read_result_reaches_the_conversation_as_the_files_text"
                f"[{case}]",
                "assert content == notes",
            )
            for case in ("record-shaped", "one-block")
        ),
    ),
    "gate-from-text": SwitchedOff(
        issue=RECORD_SHAPED_ISSUE,
        what="the approval gate: a call whose id is in a user message counts as approved",
        edits=(
            Edit(
                AGENT,
                "    answer = state.approval if state.approval_for == tool_use_id else None\n",
                "    answer = state.approval if state.approval_for == tool_use_id else None\n"
                "    if answer is None and any(  # switched off\n"
                '        tool_use_id in str(m["content"]) for m in state.messages'
                ' if m["role"] == "user"\n'
                "    ):\n"
                '        answer = "given"\n',
            ),
        ),
        stack="up",
        expect=(
            Expect(
                f"{RECORD_SHAPED}::test_text_in_a_result_does_not_answer_the_gate",
                'assert outcome == "needs_approval"',
            ),
            Expect(
                f"{RECORD_SHAPED}::test_text_in_a_result_leaves_the_tracker_empty",
                "assert tracker_issues == []",
            ),
            Expect(
                f"{RECORD_SHAPED}::test_a_result_with_record_shaped_text_leaves_the_runtime"
                "_where_plain_text_does",
                "assert conversation_b == conversation_a",
            ),
            Expect(
                f"{RECORD_SHAPED}::test_text_in_a_summary_does_not_answer_the_gate",
                'assert outcome == "needs_approval"',
            ),
            Expect(
                f"{RECORD_SHAPED}::test_text_in_a_summary_leaves_the_tracker_empty",
                "assert tracker_issues == []",
            ),
            Expect(
                f"{RECORD_SHAPED}::test_a_summary_with_record_shaped_text_leaves_the_runtime"
                "_where_a_plain_one_does",
                "assert turns_s == turns_p",
            ),
            Expect(
                f"{RECORD_SHAPED_STACK}::test_text_in_a_result_does_not_open_the_gate_in_the_stack",
                'assert before["status"] == "needs_approval"',
            ),
            Expect(
                f"{RECORD_SHAPED_STACK}::test_text_in_a_result_creates_no_issue_before_the_approval",
                "assert ours == []",
            ),
        ),
    ),
    "scope-from-text": SwitchedOff(
        issue=RECORD_SHAPED_ISSUE,
        what="the call's scope from the spec alone: an address in a user message is added to it",
        edits=(
            Edit(
                AGENT,
                "                    resolve(scope, tool.sandbox.settings.deny),"
                " lease=lease, halt=halt, stop=stop\n",
                "                    resolve(\n"
                '                        (*scope, Target(host="198.51.100.7", ports=(80,)))'
                "  # switched off\n"
                "                        if any(\n"
                '                            "198.51.100.7" in str(m["content"])\n'
                "                            for m in state.messages\n"
                '                            if m["role"] == "user"\n'
                "                        )\n"
                "                        else scope,\n"
                "                        tool.sandbox.settings.deny,\n"
                "                    ),\n"
                "                    lease=lease,\n"
                "                    halt=halt,\n"
                "                    stop=stop,\n",
            ),
        ),
        stack="up",
        expect=(
            Expect(
                f"{RECORD_SHAPED}::test_text_in_a_result_does_not_widen_the_scope",
                "assert scope == SCOPE_RECORD",
            ),
            Expect(
                f"{RECORD_SHAPED}::test_text_in_a_summary_does_not_widen_the_scope",
                "assert scope == SCOPE_RECORD",
            ),
            Expect(
                f"{RECORD_SHAPED_STACK}::test_text_in_a_result_does_not_widen_the_scope_of_a"
                "_guarded_call",
                "assert scope == SCOPE_RECORD",
            ),
        ),
    ),
    "summary-split-blocks": SwitchedOff(
        issue=RECORD_SHAPED_ISSUE,
        what="the summary as one text block: the message that opens the window parses its lines",
        edits=(
            Edit(
                "src/runtime/worker/summary.py",
                "from collections.abc import Sequence\n",
                "import json  # switched off\nfrom collections.abc import Sequence\n",
            ),
            Edit(
                "src/runtime/worker/summary.py",
                '    return {"role": "user", "content": [{"type": "text",'
                ' "text": f"{text}\\n\\n{CONTINUATION}"}]}\n',
                "    return {  # switched off\n"
                '        "role": "user",\n'
                '        "content": [\n'
                "            json.loads(line) if line.startswith('{\"type\"')"
                ' else {"type": "text", "text": line}\n'
                '            for line in f"{text}\\n\\n{CONTINUATION}".splitlines()\n'
                "            if line\n"
                "        ],\n"
                "    }\n",
            ),
        ),
        stack="none",
        expect=(
            Expect(
                f"{RECORD_SHAPED}::test_a_summary_with_record_shaped_text_opens_the_window"
                "_as_one_text_block",
                "assert message == expected",
            ),
        ),
    ),
    "guard-drop-uncounted": SwitchedOff(
        issue=RECORD_SHAPED_ISSUE,
        what="the guard's count of the packets it drops outside the scope",
        edits=(
            Edit(
                "src/runtime/scope.py",
                '    output.append("counter name dropped drop")\n',
                '    output.append("drop")  # switched off\n',
            ),
        ),
        stack="up",
        expect=(
            Expect(
                f"{RECORD_SHAPED_STACK}::test_a_connect_outside_the_scope_is_dropped_and_counted",
                'assert result["dropped_packets"] > 0',
            ),
        ),
    ),
    "one-call-off": SwitchedOff(
        issue=ONE_CALL_ISSUE,
        what="one tool call a response: the request no longer asks for it, and more are run",
        edits=(
            Edit(
                AGENT,
                "else (TOOL_CHOICE_ONE_CALL if trigger is not None else None)\n",
                "else None  # switched off\n",
            ),
            Edit(AGENT, "    if one_call and len(ids) > 1:\n", "    if False:  # switched off\n"),
        ),
        stack="none",
        expect=(
            Expect(
                f"{ONE_CALL_TESTS}"
                "::test_a_job_with_a_trigger_asks_for_one_call_in_every_request_but_a_summary",
                'assert [entry["tool_choice"] for entry in logged(app)] == expected',
            ),
            Expect(
                f"{ONE_CALL_TESTS}"
                "::test_two_calls_in_a_response_to_a_one_call_request_fail_the_job_before_a_tool_runs",
                "assert outcome == f\"failed: the model's response is not usable:"
                ' {TWO_CALLS_PROBLEM}"',
            ),
            *(
                Expect(
                    f"{ONE_CALL_TESTS}"
                    "::test_a_takeover_sends_the_tool_choice_the_first_worker_would_have"
                    f"[{answers}-{other}]",
                    "assert expected.count(ONE_CALL) == len(expected) - SUMMARIES",
                )
                for answers in (3, 4, 6)
                for other in (None, 400, 5000)
            ),
            Expect(
                "tests/unit/test_response_check.py"
                "::test_more_calls_than_the_one_asked_are_a_problem",
                'assert problem == "it has 3 tool_use blocks where one was asked"',
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
        case "long":
            if provider == "claude":
                # As `make long-job-claude`: the Claude overlay reads the key.
                compose = ["-f", "docker-compose.yml", "-f", "docker-compose.long.yml"]
                compose += ["-f", "docker-compose.claude.yml"]
                return [["docker", "compose", *compose, "up", "--build", "--wait"]]
            return [["make", "long-up"]]
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


def _load(name: str, path: str, root: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, root / path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"{path} cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    # Registered before it runs: the dataclasses in it look their module up.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def kill_rules(root: Path = ROOT) -> ModuleType:
    """`tests/kills/rules.py` of `root`, loaded by its path. It imports nothing of the stack."""
    return _load("switched_off_kill_rules", KILL_RULES, root)


def long_job_rules(root: Path = ROOT) -> ModuleType:
    """`tests/long_job/rules.py` of `root`, loaded by its path: the long bound, the short cap,
    the refusals of a keyed run. It imports nothing of the stack and no SDK."""
    return _load("switched_off_long_job_rules", LONG_JOB_RULES, root)


def long_job_grader(root: Path = ROOT) -> ModuleType:
    """`tests/long_job/grader.py` of `root`, loaded by its path: the names of its clauses."""
    return _load("switched_off_long_job_grader", LONG_JOB_GRADER, root)


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
        if not ENTRY_NAME.fullmatch(name):
            problems.append(f"{name!r}: a name is lowercase letters, digits and hyphens only")
        try:
            # A path outside the checkout's code is not read: run_problems names it.
            if not edit_path_problems(entry):
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


PYTEST_NO_TESTS = 5  # pytest's exit code when it collected nothing


def uncollected(root: Path = ROOT) -> list[str]:
    """The listed test ids, parameters included, that pytest does not collect.

    A run with an unknown id runs none of its entry's tests. `--noconftest`
    keeps this off the stack: the suites' conftests need Docker to start.
    A parameter that came from a conftest fixture would read as missing; no
    conftest has one. `PYTEST_*` variables are dropped because they change
    what pytest collects and the output read here.
    """
    nodes = {expect.node for entry in ENTRIES.values() for expect in entry.expect}
    files = sorted({node.partition("::")[0] for node in nodes})
    env = {
        name: value
        for name, value in without_keys(os.environ).items()
        if not name.startswith("PYTEST_")
    }
    done = subprocess.run(  # noqa: S603 - a fixed command and the list's own paths
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "--noconftest",
            "-p",
            "no:cacheprovider",
            f"--rootdir={root}",
            *files,
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if done.returncode != 0:
        output = [*done.stdout.splitlines(), *done.stderr.splitlines()]
        errors = "; ".join(line for line in output if line.startswith("ERROR"))
        empty = "no tests collected" if done.returncode == PYTEST_NO_TESTS else "no error line"
        return [
            f"pytest could not collect the listed tests: exit {done.returncode}: {errors or empty}"
        ]
    collected = set(done.stdout.splitlines())
    return [f"pytest collects no test {node}" for node in sorted(nodes - collected)]


def _inside(path: str, under: tuple[str, ...]) -> bool:
    """Whether a path is relative, has no `..` part, and starts with one of `under`."""
    pure = PurePosixPath(path)
    return not pure.is_absolute() and ".." not in pure.parts and path.startswith(under)


def edit_path_problems(entry: SwitchedOff) -> list[str]:
    """The edits whose path is not under `src/` or `migrations/` of the checkout."""
    return [
        f"an edit may change {' or '.join(EDITABLE)} only, not {edit.path!r}"
        for edit in entry.edits
        if not _inside(edit.path, EDITABLE)
    ]


def keyed_long(entry: SwitchedOff) -> bool:
    """Whether the entry runs the long-job harness: the long stack on the Claude API."""
    return entry.stack == "long" and entry.provider == "claude"


def stack_tests(entry: SwitchedOff) -> str | None:
    """The one test directory the entry's stack runs, or None for a stack that runs any."""
    return LONG_JOB_TESTS if keyed_long(entry) else STACK_TESTS.get(entry.stack)


def run_problems(entry: SwitchedOff, root: Path = ROOT) -> list[str]:
    """What is wrong with an entry's provider, stack, jobs and record. Empty when nothing is.

    An entry on the Claude API gets the key. So its model is one the
    workflow offers and it is judged by its record. On the kills stack the
    record is `Refusals`, and its jobs times the budget of each stay inside
    the bound. On the long stack it is `Verdicts`: the model is the
    harness's, the jobs are its two, and the grader has each clause that
    must fire. Nothing else gets the key. The long bound is not asked here:
    `bound_problem` asks it when a run starts.
    """
    problems = edit_path_problems(entry)
    kills = entry.stack == "kills"
    long = keyed_long(entry)
    if entry.provider == "claude":
        offered = options((root / WORKFLOW).read_text(), "model")
        if entry.model not in offered:
            problems.append(f"the workflow offers no model {entry.model!r}")
        if not kills and not long:
            problems.append(
                f"a run on the Claude API uses the kills or the long stack, not {entry.stack!r}"
            )
        if entry.record is None:
            problems.append("a run on the Claude API is judged by its record, and names none")
        elif kills and not isinstance(entry.record, Refusals):
            problems.append("a run on the Claude API and the kills stack is judged by `Refusals`")
        elif long and not isinstance(entry.record, Verdicts):
            problems.append("a run on the Claude API and the long stack is judged by `Verdicts`")
    elif entry.model is not None:
        problems.append(f"the fake provider takes no model, and the entry names {entry.model!r}")
    tests = stack_tests(entry)
    if tests is not None:
        outside = [
            expect.node
            for expect in entry.expect
            if not _inside(expect.node.partition("::")[0], (tests,))
        ]
        if outside:
            problems.append(f"stack {entry.stack!r} runs {tests} only, not {outside}")
    if long:
        return problems + verdicts_problems(entry, root)
    if not kills:
        if entry.runs or entry.record is not None:
            problems.append("`runs` and `record` are for an entry on the Claude API")
        return problems
    if not isinstance(entry.record, Refusals):
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


def verdicts_problems(entry: SwitchedOff, root: Path = ROOT) -> list[str]:
    """What is wrong with an entry of the long-job harness beyond its stack and provider."""
    rules = long_job_rules(root)
    problems = []
    if entry.model != rules.MODEL:
        problems.append(f"the long-job harness runs model {rules.MODEL}, not {entry.model!r}")
    if entry.runs != rules.MUST_FAIL_JOBS:
        problems.append(
            f"`runs` is {entry.runs}, not {rules.MUST_FAIL_JOBS}: the control and the edited job"
        )
    if isinstance(entry.record, Verdicts):
        grader = long_job_grader(root)
        missing = [name for name in entry.record.fires if name not in grader.CLAUSES]
        if missing or not entry.record.fires:
            problems.append(f"{LONG_JOB_GRADER} has no clause {missing}, or none is named")
        if entry.record.control not in grader.VERDICTS:
            problems.append(f"{LONG_JOB_GRADER} has no verdict {entry.record.control!r}")
    return problems


def bound_problem(entry: SwitchedOff, root: Path = ROOT) -> str | None:
    """Why an entry of the long-job harness may not start a run: the long bound. Else None.

    The bound lives in `tests/long_job/rules.py`. While it is not set, or
    the entry's jobs times the short cap exceed it, no such run starts.
    `run` and `_dispatch` ask it; `check`, `run_problems` and a rehearsal
    do not, so the list's check stays green while it is not set.
    """
    if not isinstance(entry.record, Verdicts):
        return None
    rules = long_job_rules(root)
    bound = rules.LONG_KEYED_TOKEN_BOUND
    if bound is None or entry.runs * rules.SHORT.budget_cap > bound:
        sentence: str = rules.NO_BOUND
        return sentence
    return None


def outcomes(junit: Path) -> dict[str, tuple[str, str]]:
    """Each test case in a junit file: node id -> (passed | failed | error | skipped, text).

    A test that failed and whose teardown failed too has two cases. The
    later one counts: an error.
    """
    found: dict[str, tuple[str, str]] = {}
    for case in ET.parse(junit).getroot().iter("testcase"):  # noqa: S314 - pytest wrote it
        node = _node(case)
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


def _node(case: ET.Element) -> str:
    """The node id of a junit test case."""
    path = case.get("classname", "").replace(".", "/") + ".py"
    return f"{path}::{case.get('name', '')}"


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
    if not isinstance(entry.record, Refusals) or entry.model is None:
        raise ValueError("only an entry with `Refusals` and a model is judged by its record")
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


@dataclass(frozen=True)
class Phase:
    """One phase of a run of the long-job harness: `judge`'s lines, the record it left, and
    whether its answers test found an answer of the job."""

    name: str
    lines: tuple[str, ...]
    records: tuple[dict[str, Any], ...]
    trouble: tuple[str, ...]
    leaked: bool = False


# A clause in either job's record that makes the run a finding whatever else
# the record holds: the runtime's own failures.
RUNTIME_CLAUSES = ("R2", "R5", "R6", "crash", "runtime")
# The edited job's stops before its end: a finding. It must be graded on R7, R8 and R9.
STOP_CLAUSES = ("R1", "no_class")
# Reported for the edited job, not judged: the control showed the kills land.
REPORTED_CLAUSES = ("kill_failpoint", "kill_2", "kill_3", "kill_windows", "takeovers")
ANSWERS_KEPT_OUT = "no answer of the job is printed, recorded or kept"


def phase_entry(entry: SwitchedOff, phase: str) -> SwitchedOff:
    """The entry as `judge` reads one phase of its run.

    In the control every test passes. In the edited phase the tests end as
    the entry lists them. In both the answers test runs too, and passes.
    """
    expect = entry.expect
    if phase == "control":
        expect = tuple(
            replace(e, fails_with=None, passes_because="the control runs the checkout as it is")
            for e in expect
        )
    return replace(entry, expect=(*expect, Expect(LONG_JOB_ANSWERS, None, ANSWERS_KEPT_OUT)))


def answers_leaked(junit: Path) -> bool:
    """Whether the answers test failed on its assert that found an answer of the job.

    Read from every case of that test in the junit: `outcomes` keeps one per
    node, and a teardown error after the failure would hide it. Its other
    failures, an error alone and a test that did not run show no leak.
    """
    if not junit.is_file():
        return False
    failures = [
        f"{failure.get('message', '')}\n{failure.text or ''}"
        for case in ET.parse(junit).getroot().iter("testcase")  # noqa: S314 - pytest wrote it
        if _node(case) == LONG_JOB_ANSWERS
        for failure in case.findall("failure")
    ]
    return any(ANSWER_FOUND in text for text in failures)


def _clauses(record: Mapping[str, Any]) -> dict[str, int]:
    found = record.get("clauses")
    if not isinstance(found, dict):
        return {}
    return {
        str(name): count
        for name, count in found.items()
        if isinstance(count, int) and not isinstance(count, bool) and count > 0
    }


def _listed(record: Mapping[str, Any], names: tuple[str, ...]) -> list[str]:
    held = _clauses(record)
    return [name for name in names if name in held]


def _runtime(record: Mapping[str, Any]) -> list[str]:
    """The runtime's own failures a record names: its clauses, and a worker's crash record even
    where the job could not be graded."""
    found = _listed(record, RUNTIME_CLAUSES)
    if record.get("crashed") is True and "crash" not in found:
        found.append("crash")
    return found


def _void(record: Mapping[str, Any]) -> bool:
    voided = any(name.startswith("void_") for name in _clauses(record))
    return record.get("verdict") == "VOID" or voided or bool(record.get("voids"))


def judge_verdicts(
    entry: SwitchedOff,
    phases: Mapping[str, Phase],
    *,
    head: str,
    provider: str,
    rules: ModuleType,
    control_only: bool = False,
) -> tuple[str, list[str]]:
    """PASS, FINDING or INCONCLUSIVE for a run of the long-job harness, and the reasons.

    The first of these that holds gives the verdict:

    1. The answers test of either phase found an answer of the job:
       FINDING, whatever else happened.
    2. Either job's record lists R2, R5, R6, a crash or a runtime failure:
       FINDING, even beside a void.
    3. A phase that ran left no record of this run (its commit, provider,
       model, mode, phase and one job): INCONCLUSIVE.
    4. Either record is void: INCONCLUSIVE.
    5. The control's verdict is not the entry's, or a test of the control
       did not end as listed: FINDING. The edit was never written.
    6. The edited job did not run: INCONCLUSIVE.
    7. The edited record does not list each clause that must fire, misses
       a floor, lists R1 or NO_CLASS, or a test of it did not end as
       listed: FINDING.
    8. PASS.

    With `control_only` the judge stops after rule 5: the control half,
    which `run` asks before it writes the edit, and a rehearsal asks alone.
    A missed kill in the edited job, too few takeovers included, is
    reported, not judged.
    """
    if not isinstance(entry.record, Verdicts) or entry.model is None:
        raise ValueError("only an entry with `Verdicts` and a model is judged by two jobs")
    label = "Claude API" if provider == "claude" else "fake provider"
    model = entry.model if provider == "claude" else rules.FAKE_MODEL
    asked = ("control",) if control_only else ("control", "edited")
    records: dict[str, Mapping[str, Any]] = {}
    missing: list[str] = []
    for name in asked:
        if name not in phases:
            continue
        phase = phases[name]
        if phase.trouble or not phase.records:
            missing += [f"{name}: no record to judge: {problem}" for problem in phase.trouble]
            continue
        record = phase.records[0]
        wrong = not_this_run(record, runs=1, provider=label, model=model, head=head)
        for key, wanted in (("mode", "must-fail"), ("phase", name)):
            if record.get(key) != wanted:
                wrong.append(f"the record's {key} is {record.get(key)!r}, not {wanted!r}")
        if wrong:
            missing += [f"{name}: {problem}" for problem in wrong]
            continue
        records[name] = record
    lines = [
        f"{name}: verdict {record.get('verdict')}, clauses "
        + (", ".join(f"{c} {n}" for c, n in _clauses(record).items()) or "none")
        for name, record in records.items()
    ]
    broken = [
        f"{name}: the runtime failed the job: {', '.join(found)}"
        for name, record in records.items()
        if (found := _runtime(record))
    ]
    leaks = [
        f"{name}: an answer of the job is in what the session printed, recorded or kept"
        for name in asked
        if name in phases and phases[name].leaked
    ]
    if leaks:
        return "FINDING", [*lines, *leaks, *broken, *missing]
    if broken:
        return "FINDING", [*lines, *broken]
    if missing:
        return "INCONCLUSIVE", [*lines, *missing]
    voided = [name for name, record in records.items() if _void(record)]
    if voided:
        return "INCONCLUSIVE", [*lines, f"{' and '.join(voided)}: the run is void"]
    if "control" not in records:
        return "INCONCLUSIVE", [*lines, "the control job did not run"]
    as_listed = all(line.startswith("ok") for line in phases["control"].lines)
    if records["control"].get("verdict") != entry.record.control or not as_listed:
        return "FINDING", [*lines, "the control job did not pass: the edit was not written"]
    if control_only:
        return "PASS", [*lines, "the control job passed"]
    if "edited" not in records:
        return "INCONCLUSIVE", [*lines, "the edited job did not run"]
    edited = records["edited"]
    fires = entry.record.fires
    reasons = []
    if quiet := [name for name in fires if name not in _clauses(edited)]:
        reasons.append(f"edited: did not fire: {', '.join(quiet)}")
    if floors := [name for name in _clauses(edited) if name.startswith("floor_")]:
        reasons.append(f"edited: a floor is missed: {', '.join(floors)}")
    if stopped := _listed(edited, STOP_CLAUSES):
        reasons.append(f"edited: the job did not run to its end: {', '.join(stopped)}")
    if not all(line.startswith("ok") for line in phases["edited"].lines):
        reasons.append("edited: a test did not end as listed")
    reported = [
        f"edited, reported and not judged: {', '.join(found)}"
        for found in [_listed(edited, REPORTED_CLAUSES)]
        if found
    ]
    if reasons:
        return "FINDING", [*lines, *reasons, *reported]
    passed = f"the control passed, and the edited job fails on {', '.join(fires)} with every floor"
    return "PASS", [*lines, passed, *reported]


def names(provider: str) -> list[str]:
    """The entries of one provider, in the list's order."""
    return [name for name, entry in ENTRIES.items() if entry.provider == provider]


# The workflow job that runs an entry of the long-job harness.
LONG_JOB_JOB = "long-job"


def dispatch_problem(
    name: str, provider: str, model: str | None, job: str | None = None
) -> str | None:
    """Why the workflow's inputs do not start this entry, or None.

    The entry names its provider and model. The inputs have to say the
    same, so the job that runs and its name are the entry's. An entry of the
    long-job harness runs only in the job `long-job`, which says so with
    `job`, and that job runs no other entry.
    """
    entry = ENTRIES[name]
    if provider != entry.provider:
        return f"{name} runs on provider {entry.provider}, not {provider}"
    if entry.provider == "claude" and model != entry.model:
        return f"{name} runs on model {entry.model}, not {model}"
    if keyed_long(entry) and job != LONG_JOB_JOB:
        return f"{name} runs in the workflow's {LONG_JOB_JOB} job only"
    if not keyed_long(entry) and job is not None:
        return f"{name} does not run in the workflow's {job} job"
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


def child_environment(
    entry: SwitchedOff, provider: str, env: Mapping[str, str], phase: str = "control"
) -> dict[str, str]:
    """The environment of the stack and the tests of a run, made from the entry.

    No inherited `KILLS_*` or `LONG_JOB_*` variable and no failpoint reaches
    them: the entry alone names the provider, the model and the number of
    jobs. The key is passed on only to a run on the Claude API.

    No inherited setting of the long stack reaches them either. A run on the
    long stack gets each as the empty string, and the failpoint too. A value
    in `.env` cannot replace a variable that is set: the stack starts on its
    defaults, and each test sets its own.

    An entry of the long-job harness gets the harness's provider, mode
    `must-fail` and `phase`, and no budget: the harness takes the short cap.
    On the Claude API it also gets the seed, LONG_JOB_SEED, and nothing on
    the fake provider. No `ANTHROPIC_*` variable reaches it.
    """
    dropped = ("FAILPOINT", PROVIDER_VARIABLE, *LONG_VARIABLES)
    kept = {
        name: value
        for name, value in without_keys(env).items()
        if not name.startswith(("KILLS_", "LONG_JOB_")) and name not in dropped
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
    if entry.stack == "long":
        # The failpoint too: the long stack reads it from the shell, and a
        # value in `.env` would stop a worker of the run.
        for name in ("FAILPOINT", *LONG_VARIABLES):
            kept[name] = ""
    if isinstance(entry.record, Verdicts):
        kept = {name: value for name, value in kept.items() if not name.startswith("ANTHROPIC_")}
        kept["LONG_JOB_PROVIDER"] = provider
        kept["LONG_JOB_MODE"] = "must-fail"
        kept["LONG_JOB_PHASE"] = phase
        if provider == "claude" and "LONG_JOB_SEED" in env:
            kept["LONG_JOB_SEED"] = env["LONG_JOB_SEED"]
    return kept


def _sh(*args: str, env: Mapping[str, str]) -> None:
    print("+", " ".join(args), flush=True)
    subprocess.run(args, cwd=ROOT, check=True, env=dict(env))  # noqa: S603 - fixed commands


def _say(text: str) -> None:
    """Print a line of the run's report, with its workflow-command markers escaped."""
    print(text_safe(text), flush=True)


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
    # The long bound first: while it is not set, nothing else is asked.
    bound = None if rehearsal else bound_problem(entry)
    if bound is not None:
        print(f"not started: {bound}", file=sys.stderr)
        return 2
    refused = start_problem(name, os.environ.get(PROVIDER_VARIABLE, "fake"), rehearsal=rehearsal)
    if refused is not None:
        print(f"not started: {refused}", file=sys.stderr)
        return 2
    # CI checks the list on every PR. A run checks its own entry again.
    wrong = run_problems(entry)
    if wrong:
        print(f"not started: {'; '.join(wrong)}", file=sys.stderr)
        return 2
    if isinstance(entry.record, Verdicts):
        return run_verdicts(name, entry, rehearsal=rehearsal)
    provider = "fake" if rehearsal else entry.provider
    if rehearsal:
        _say(f"rehearsal of {name} on the fake provider: not a result")
    _say(f"switched off: {name} (issue {entry.issue}): {entry.what}")
    changed = _git("status", "--porcelain", "--untracked-files=no")
    if changed:
        _say(f"WRONG: the checkout has changes; the run would not be HEAD's code:\n{changed}")
        return 1
    head = _git("rev-parse", "HEAD").strip()
    _say(f"commit {head}")
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
    _say(f"pytest exit {tests.returncode}")
    found = outcomes(ROOT / JUNIT) if (ROOT / JUNIT).is_file() else {}
    lines = judge(entry, found) if found else ["WRONG: pytest wrote no results"]
    if entry.record is None:
        _say("\n".join(lines))
        return 1 if any(line.startswith("WRONG") for line in lines) else 0
    records, trouble = new_records(kept, before, entry.record.test)
    rules = kill_rules()
    if rehearsal:
        verdict, reasons = judge_rehearsal(entry, found, records, trouble, head=head, rules=rules)
    else:
        _say("\n".join(lines))
        verdict, reasons = judge_record(entry, lines, records, trouble, head=head, rules=rules)
    _say("\n".join(reasons))
    _say(f"{verdict}: {name}")
    return 0 if verdict in ("PASS", "REHEARSAL OK") else 1


def run_verdicts(name: str, entry: SwitchedOff, *, rehearsal: bool) -> int:
    """Run an entry of the long-job harness: the control, and only after it passed, the edit.

    On the Claude API the harness's own refusals of the run are asked
    first, and the edit is matched, before any stack starts. A rehearsal runs the control on the
    fake provider with no edit, no key and no seed, and stops there.
    """
    provider = "fake" if rehearsal else entry.provider
    rules = long_job_rules()
    if not rehearsal:
        refused = rules.refusals(child_environment(entry, provider, os.environ), ROOT)
        if refused:
            print(f"not started: {' '.join(refused)}", file=sys.stderr)
            return 2
        _say(f"switched off: {name} (issue {entry.issue}): {entry.what}")
    else:
        _say(f"rehearsal of {name} on the fake provider, the control job only: not a result")
    changed = _git("status", "--porcelain", "--untracked-files=no")
    if changed:
        _say(f"WRONG: the checkout has changes; the run would not be HEAD's code:\n{changed}")
        return 1
    head = _git("rev-parse", "HEAD").strip()
    _say(f"commit {head}")
    texts: dict[str, str] = {}
    if not rehearsal:
        # Matched before any job: a miss found after the control would waste a paid job.
        try:
            texts = edited_files(entry.edits, ROOT)
        except ValueError as error:
            print(f"not started: {error}", file=sys.stderr)
            return 2
    phases = {"control": run_phase(entry, provider, "control")}
    verdict, reasons = judge_verdicts(
        entry, phases, head=head, provider=provider, rules=rules, control_only=True
    )
    if rehearsal:
        _say("\n".join([*reasons, f"control: {verdict}"]))
        verdict = "REHEARSAL OK" if verdict == "PASS" else "REHEARSAL WRONG"
        _say(f"{verdict}: {name}")
        return 0 if verdict == "REHEARSAL OK" else 1
    if verdict == "PASS":
        for path, text in texts.items():
            (ROOT / path).write_text(text)
        _sh("git", "--no-pager", "diff", "--", *texts, env=without_keys(os.environ))
        phases["edited"] = run_phase(entry, provider, "edited")
        verdict, reasons = judge_verdicts(entry, phases, head=head, provider=provider, rules=rules)
    _say("\n".join(reasons))
    _say(f"{verdict}: {name}")
    return 0 if verdict == "PASS" else 1


def run_phase(entry: SwitchedOff, provider: str, phase: str) -> Phase:
    """One phase: start the stack, run the entry's tests and the answers test, read the record."""
    env = child_environment(entry, provider, os.environ, phase)
    for command in stack_commands(entry.stack, provider):
        _sh(*command, env=env)
    junit = JUNIT.with_name(f"junit-{phase}.xml")
    (ROOT / junit).parent.mkdir(exist_ok=True)
    (ROOT / junit).unlink(missing_ok=True)
    kept = ROOT / LONG_JOB_RECORDS
    before = {found.name for found in kept.glob("*")}
    checked = phase_entry(entry, phase)
    nodes = [expect.node for expect in checked.expect]
    pytest = ["uv", "run", "--frozen", "pytest", *LONG_JOB_PYTEST, "-p", "no:cacheprovider"]
    # A fixed command; the node ids come from the list above.
    tests = subprocess.run(  # noqa: S603
        [*pytest, f"--junitxml={junit}", *nodes], cwd=ROOT, check=False, env=env
    )
    _say(f"{phase}: pytest exit {tests.returncode}")
    found = outcomes(ROOT / junit) if (ROOT / junit).is_file() else {}
    lines = judge(checked, found) if found else ["WRONG: pytest wrote no results"]
    _say("\n".join(f"{phase}: {line}" for line in lines))
    records, trouble = new_records(kept, before, LONG_JOB_TEST)
    leaked = answers_leaked(ROOT / junit)
    return Phase(phase, tuple(lines), tuple(records), tuple(trouble), leaked)


def main(argv: list[str]) -> int:
    match argv:
        case ["names"]:
            print("\n".join(ENTRIES))
            return 0
        case ["names", "fake"]:
            print(json.dumps(names("fake")))
            return 0
        case ["check"]:
            problems = [*check(), *uncollected()]
            print(
                "\n".join(problems)
                or "every edit matches once and compiles, and pytest collects every listed test"
            )
            return 1 if problems else 0
        case ["dispatch", name, provider] if name in ENTRIES:
            return _dispatch(name, provider, None)
        case ["dispatch", name, provider, model] if name in ENTRIES:
            return _dispatch(name, provider, model)
        case ["dispatch", name, provider, model, job] if name in ENTRIES:
            return _dispatch(name, provider, model, job)
        case ["run", name] if name in ENTRIES:
            return run(name)
        case ["rehearse", name] if name in ENTRIES:
            return run(name, rehearsal=True)
        case ["run" | "rehearse" | "dispatch", _, *_]:
            print(f"not a switched-off run; one of: {', '.join(ENTRIES)}", file=sys.stderr)
            return 2
    print(__doc__, file=sys.stderr)
    return 2


def _dispatch(name: str, provider: str, model: str | None, job: str | None = None) -> int:
    problem = dispatch_problem(name, provider, model, job)
    if problem is None and (wrong := run_problems(ENTRIES[name])):
        problem = "; ".join(wrong)
    if problem is None:
        problem = bound_problem(ENTRIES[name])
    if problem is not None:
        print(f"not started: {problem}", file=sys.stderr)
        return 1
    print(f"{name}: provider {provider}" + (f", model {model}" if provider == "claude" else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
