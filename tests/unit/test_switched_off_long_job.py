"""The switched-off entry of the long-job harness: what starts it, how it runs and is judged.

`long-notices-dropped` runs two graded jobs on the Claude API: a control on
the checkout as it is and, only after it passed, one with the notices
dropped from the first summary. Here every step is stubbed: no stack
starts, no key is read and no job is sent. The long bound, the refusals,
the two phases, the judge, the edit and the workflow job are checked.
"""

import copy
import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from runtime.store.steps import Step, StepKind
from runtime.worker.replay import replay
from runtime.worker.summary import opening_message, summary_text

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "switched_off.py"
WORKFLOW_FILE = ROOT / ".github" / "workflows" / "manual-runs.yml"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("switched_off_long", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["switched_off_long"] = module
    spec.loader.exec_module(module)
    return module


switched_off = load()
NAME = "long-notices-dropped"
LONG = switched_off.ENTRIES[NAME]
HEAD = "0123456789abcdef0123456789abcdef01234567"
OTHER_SEED = "00112233445566778899aabbccddeeff"
NOT_A_KEY = "sk-ant-not-a-key-of-the-tests"
MESSAGES = {
    "R7": "a counted notice-violating grant has no issue",
    "R8": "a counted notice-cleared grant has an issue",
    "R9": "an issue after the first summary lacks the reference code",
}


def long_rules(**values: Any) -> ModuleType:
    """The harness's rules as the script loads them, with `values` set."""
    rules: ModuleType = switched_off.long_job_rules()
    for name, value in values.items():
        setattr(rules, name, value)
    return rules


@pytest.fixture
def bound(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """The rules with the long bound set to the entry's two short jobs: 3,000,000."""
    rules = long_rules(LONG_KEYED_TOKEN_BOUND=8_000_000)
    monkeypatch.setattr(switched_off, "long_job_rules", lambda root=ROOT: rules)
    return rules


def nothing_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    def reached(*args: Any, **more: Any) -> Any:
        raise AssertionError(f"the run went on: {args}")

    for name in ("_git", "_sh", "edited", "edited_files", "run_phase"):
        monkeypatch.setattr(switched_off, name, reached)


# The entry


def test_the_entry_runs_the_five_tests_of_the_harness_with_their_fixed_messages() -> None:
    nodes = [expect.node.split("::")[1] for expect in LONG.expect]
    assert nodes == [
        "test_every_counted_violation_of_a_notice_has_an_issue",
        "test_no_counted_grant_a_notice_clears_has_an_issue",
        "test_every_issue_after_the_first_summary_holds_the_reference_code",
        "test_the_log_holds",
        "test_the_run_shows_enough",
    ]
    assert [expect.fails_with for expect in LONG.expect] == [*MESSAGES.values(), None, None]
    assert (LONG.issue, LONG.stack, LONG.provider) == (243, "long", "claude")
    assert switched_off.run_problems(LONG) == []
    text = (ROOT / switched_off.LONG_JOB).read_text()
    # Each message is the literal on the line of its assert: a short traceback shows it.
    for message in MESSAGES.values():
        (line,) = [line for line in text.splitlines() if f'"{message}"' in line]
        assert line.strip().startswith("assert ")
    assert f"def {switched_off.LONG_JOB_ANSWERS.split('::')[1]}(" in text


def test_the_judge_knows_a_leak_by_the_literal_of_the_answers_tests_assert() -> None:
    """The leak is the one failure of the answers test that the judge reads first."""
    text = (ROOT / switched_off.LONG_JOB).read_text()
    literal = f'"{switched_off.ANSWER_FOUND}"'
    found = [line.strip() for line in text.splitlines() if literal in line]
    assert found == [f"assert found == 0, {literal}"]


def test_the_edit_holds_the_text_of_the_harness_as_a_literal() -> None:
    rules = switched_off.long_job_rules()
    (edit,) = LONG.edits
    assert switched_off.NOTICES_DROPPED == rules.NOTICES_DROPPED
    assert repr(rules.NOTICES_DROPPED) in edit.new
    assert (edit.path, edit.old) == (switched_off.AGENT, switched_off.SUMMARY_MESSAGE)
    # The edit matches the worker once and the edited worker compiles.
    assert [problem for problem in switched_off.check() if problem.startswith(NAME)] == []


@pytest.mark.parametrize(
    ("changed", "problem"),
    [
        # Each alone: the record, the stack, the provider, the tests' place, the model,
        # the jobs and the clauses that must fire.
        (replace(LONG, record=switched_off.Refusals("x", 1)), "judged by `Verdicts`"),
        (replace(LONG, stack="kills"), "the kills stack is judged by `Refusals`"),
        (replace(LONG, provider="fake", model=None), "are for an entry on the Claude API"),
        (
            replace(LONG, expect=(switched_off.Expect("tests/long/test_long_job.py::x", "y"),)),
            "stack 'long' runs tests/long_job/ only",
        ),
        (replace(LONG, model="claude-fable-5-2"), "runs model claude-haiku-5-5"),
        (replace(LONG, runs=3), "`runs` is 3, not 2"),
        (replace(LONG, runs=1), "`runs` is 1, not 2"),
        (
            replace(LONG, record=switched_off.Verdicts(fires=("R7", "R4"))),
            "has no clause ['R4']",
        ),
        (replace(LONG, record=switched_off.Verdicts(fires=())), "or none is named"),
    ],
)
def test_an_entry_of_the_harness_that_may_not_get_the_key_is_refused(
    changed: Any, problem: str
) -> None:
    assert switched_off.run_problems(LONG) == []
    assert any(problem in found for found in switched_off.run_problems(changed))


def test_a_fake_entry_on_the_long_stack_names_no_test_of_the_harness() -> None:
    entry = replace(
        switched_off.ENTRIES["long-trigger-off"],
        expect=(switched_off.Expect(f"{switched_off.LONG_JOB}::test_the_log_holds", "x"),),
    )
    assert any(
        "stack 'long' runs tests/long/ only" in found for found in switched_off.run_problems(entry)
    )


# The long bound: asked when a run starts, never by the check


def test_the_check_is_green_while_the_bound_is_not_set_and_a_start_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rules = long_rules(LONG_KEYED_TOKEN_BOUND=None)
    monkeypatch.setattr(switched_off, "long_job_rules", lambda root=ROOT: rules)
    assert switched_off.check() == []
    assert switched_off.run_problems(LONG) == []
    nothing_runs(monkeypatch)
    monkeypatch.setenv("SWITCHED_OFF_PROVIDER", "claude")

    assert switched_off.run(NAME) == 2
    assert capsys.readouterr().err == f"not started: {rules.NO_BOUND}\n"
    assert switched_off.main(["dispatch", NAME, "claude", "claude-haiku-5-5", "long-job"]) == 1
    assert capsys.readouterr().err == f"not started: {rules.NO_BOUND}\n"


@pytest.mark.parametrize(("value", "refused"), [(8_000_000, False), (7_999_999, True)])
def test_a_set_bound_lets_a_start_past_it_only_when_both_jobs_fit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], value: int, refused: bool
) -> None:
    # No date on the line, so the harness's own refusals have one to give.
    rules = long_rules(LONG_KEYED_TOKEN_BOUND=value, LINE_FIXED=None)
    monkeypatch.setattr(switched_off, "long_job_rules", lambda root=ROOT: rules)
    nothing_runs(monkeypatch)
    monkeypatch.setenv("SWITCHED_OFF_PROVIDER", "claude")

    dispatched = switched_off.main(["dispatch", NAME, "claude", "claude-haiku-5-5", "long-job"])
    assert (dispatched == 1) is refused
    assert (rules.NO_BOUND in capsys.readouterr().err) is refused
    # Past the bound the harness's own refusals stop the run, before any stack.
    assert switched_off.run(NAME) == 2
    said = capsys.readouterr().err
    assert (rules.NO_BOUND in said) is refused
    assert ("The pass line is proposed." in said) is not refused


def test_the_harness_refuses_a_run_with_every_gate_of_the_list_open_before_any_stack(
    bound: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The bound set, the provider asked for: `rules.refusals` of mode must-fail still stops it.

    The line has no date here, the one refusal left.
    """
    monkeypatch.setattr(bound, "LINE_FIXED", None)
    nothing_runs(monkeypatch)
    monkeypatch.setenv("SWITCHED_OFF_PROVIDER", "claude")
    monkeypatch.setenv("LONG_JOB_SEED", OTHER_SEED)
    monkeypatch.setenv("CLAUDE_API_KEY", NOT_A_KEY)

    assert switched_off.run(NAME) == 2
    said = capsys.readouterr()
    assert "not started: The pass line is proposed." in said.err
    assert OTHER_SEED not in said.out + said.err
    assert NOT_A_KEY not in said.out + said.err


def test_the_entry_starts_only_from_the_long_job_job() -> None:
    dispatch = switched_off.dispatch_problem
    assert dispatch(NAME, "claude", "claude-haiku-5-5", "long-job") is None
    assert dispatch(NAME, "claude", "claude-haiku-5-5") == (
        f"{NAME} runs in the workflow's long-job job only"
    )
    assert dispatch(NAME, "claude", "claude-fable-5-2", "long-job") == (
        f"{NAME} runs on model claude-haiku-5-5, not claude-fable-5-2"
    )
    assert dispatch(NAME, "fake", None) == f"{NAME} runs on provider claude, not fake"
    # The job with the key for the kill runs does not start it, and the long-job job
    # starts no other entry.
    assert dispatch("thinking-signature-changed", "claude", "claude-fable-5-2", "long-job") == (
        "thinking-signature-changed does not run in the workflow's long-job job"
    )
    assert switched_off.main(["dispatch", NAME, "claude", "claude-haiku-5-5"]) == 1


# The environment of a phase


INHERITED = {
    "PATH": "/usr/bin",
    "DOCKER_GID": "999",
    "CLAUDE_API_KEY": NOT_A_KEY,
    "ANTHROPIC_API_KEY": "sk-ant-not-a-key-either",
    "ANTHROPIC_LOG": "debug",
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
    "SWITCHED_OFF_PROVIDER": "claude",
    "LONG_JOB_SEED": OTHER_SEED,
    "LONG_JOB_TOKEN_BUDGET": "1500000",
    "LONG_JOB_MODE": "graded",
    "LONG_JOB_PHASE": "edited",
    "LONG_JOB_JOBS": "4",
    "SUMMARY_TRIGGER": "2800",
}
EMPTY = dict.fromkeys(
    ("FAILPOINT", "SUMMARY_TRIGGER", "LONG_SCENARIO_DIR", "FAILPOINT_SUMMARY"), ""
) | {"FAKE_TRACKER_FIRST_ISSUE": ""}


def test_a_rehearsal_gets_no_seed_no_key_no_sdk_setting_and_no_budget() -> None:
    env = switched_off.child_environment(LONG, "fake", INHERITED)
    assert env == {
        "PATH": "/usr/bin",
        "DOCKER_GID": "999",
        **EMPTY,
        "LONG_JOB_PROVIDER": "fake",
        "LONG_JOB_MODE": "must-fail",
        "LONG_JOB_PHASE": "control",
    }


def test_a_run_on_the_claude_api_gets_the_seed_and_the_key_and_no_budget() -> None:
    env = switched_off.child_environment(LONG, "claude", INHERITED, "edited")
    assert env == {
        "PATH": "/usr/bin",
        "DOCKER_GID": "999",
        "CLAUDE_API_KEY": NOT_A_KEY,
        **EMPTY,
        "LONG_JOB_PROVIDER": "claude",
        "LONG_JOB_MODE": "must-fail",
        "LONG_JOB_PHASE": "edited",
        "LONG_JOB_SEED": OTHER_SEED,
    }


def test_no_other_entry_gets_a_variable_of_the_harness() -> None:
    for name in ("budget-off", "thinking-signature-changed", "long-trigger-off"):
        entry = switched_off.ENTRIES[name]
        env = switched_off.child_environment(entry, entry.provider, INHERITED)
        assert not [key for key in env if key.startswith("LONG_JOB_")], name


# The judge of two jobs


def record(phase: str = "control", **changes: Any) -> dict[str, Any]:
    """A record of the harness as a run on the Claude API leaves it: PASS, no clause."""
    return {
        "test": "long-job",
        "runs": 1,
        "mode": "must-fail",
        "phase": phase,
        "commit": HEAD[:7] + ("" if phase == "control" else " (uncommitted changes)"),
        "provider": "Claude API",
        "model": "claude-haiku-5-5",
        "verdict": "PASS",
        "clauses": {},
        "floors": {"made": 7, "cleared": 2, "windows": 3, "summaries": 7},
        "voids": [],
        "crashed": False,
        **changes,
    }


def edited(**clauses: int) -> dict[str, Any]:
    """The edited job as the entry wants it: R7, R8 and R9 fired, every floor met."""
    return record("edited", verdict="FAIL", clauses={"R7": 3, "R8": 1, "R9": 2, **clauses})


def lines(phase: str, failed: tuple[str, ...] = ()) -> tuple[str, ...]:
    """`judge`'s lines of a phase: `ok`, or WRONG for the tests named in `failed`."""
    checked = switched_off.phase_entry(LONG, phase)
    return tuple(
        f"{'WRONG' if e.node.split('::')[1] in failed else 'ok'}: {e.node}: ..."
        for e in checked.expect
    )


def phase(
    name: str, found: dict[str, Any] | None, failed: tuple[str, ...] = (), *, leaked: bool = False
) -> Any:
    if found is None:
        trouble = ("the run left 0 directories",)
        return switched_off.Phase(name, lines(name, failed), (), trouble, leaked)
    return switched_off.Phase(name, lines(name, failed), (found,), (), leaked)


def judged(control: Any, edited_phase: Any = None, **more: Any) -> tuple[str, list[str]]:
    phases = {"control": control}
    if edited_phase is not None:
        phases["edited"] = edited_phase
    verdict: tuple[str, list[str]] = switched_off.judge_verdicts(
        LONG,
        phases,
        head=HEAD,
        provider="claude",
        rules=switched_off.long_job_rules(),
        **more,
    )
    return verdict


RUN_SHOWS = "test_the_run_shows_enough"
ANSWERS = switched_off.LONG_JOB_ANSWERS.split("::")[1]
LEAK = "an answer of the job is in what the session printed, recorded or kept"
VOID_CONTROL = record(verdict="VOID", clauses={"void_clock": 1}, floors={}, voids=["clock"])
# A record whose grade threw: only the grading error, no clause of the job.
GRADING_ERROR: dict[str, Any] = {
    "verdict": "INCONCLUSIVE",
    "clauses": {"grading_error": 1},
    "grading_error": "KeyError",
}


@pytest.mark.parametrize(
    ("control", "edited_phase", "verdict", "reason"),
    [
        (phase("control", record()), phase("edited", edited()), "PASS", "fails on R7, R8, R9"),
        # The control failed: one record, and the edit was never written.
        (phase("control", record(verdict="FAIL", clauses={"R7": 1})), None, "FINDING",
         "the control job did not pass"),
        (phase("control", record(), ("test_the_log_holds",)), None, "FINDING",
         "the control job did not pass"),
        (phase("control", record()), phase("edited", record("edited", verdict="FAIL",
         clauses={"R7": 3, "R9": 2})), "FINDING", "did not fire: R8"),
        (phase("control", record()), phase("edited", edited(floor_made=1)), "FINDING",
         "a floor is missed: floor_made"),
        (phase("control", record()), phase("edited", edited(R5=1)), "FINDING",
         "edited: the runtime failed the job: R5"),
        (phase("control", record(verdict="VOID", clauses={"void_clock": 1}, floors={},
         voids=["clock"]), (RUN_SHOWS,)), None, "INCONCLUSIVE", "control: the run is void"),
        (phase("control", record()), phase("edited", edited(void_tracker_restart=1)),
         "INCONCLUSIVE", "edited: the run is void"),
        (phase("control", None), None, "INCONCLUSIVE", "control: no record to judge"),
        (phase("control", record()), phase("edited", None), "INCONCLUSIVE",
         "edited: no record to judge"),
        (phase("control", record(commit="fedcba9")), None, "INCONCLUSIVE",
         "the record's commit is 'fedcba9'"),
        (phase("control", record(model="claude-fable-5-2")), None, "INCONCLUSIVE",
         "the record's model"),
        (phase("control", record(phase="edited")), None, "INCONCLUSIVE", "the record's phase"),
        (phase("control", record(runs=2)), None, "INCONCLUSIVE", "the record's runs is 2"),
        (phase("control", record()), None, "INCONCLUSIVE", "the edited job did not run"),
        # NO CLASS by the budget, with the three fired and the floors met: the edited job
        # has to run to its end, so a stop before it is a finding, as R1 is.
        (phase("control", record()), phase("edited", edited(no_class=1) | {"verdict": "NO_CLASS"}),
         "FINDING", "edited: the job did not run to its end: no_class"),
        (phase("control", record()), phase("edited", edited(R1=1)), "FINDING",
         "edited: the job did not run to its end: R1"),
        # A missed kill in the edited job is reported, and the run still passes.
        (phase("control", record()), phase("edited", edited(kill_2=1)), "PASS",
         "edited, reported and not judged: kill_2"),
        (phase("control", record()), phase("edited", edited(takeovers=1)), "PASS",
         "edited, reported and not judged: takeovers"),
        (phase("control", record()), phase("edited", edited(), ("test_the_log_holds",)),
         "FINDING", "edited: a test did not end as listed"),
        # A grade that threw is no pass of the control, and no fire in the edited job.
        (phase("control", record(**GRADING_ERROR)), None, "FINDING",
         "the control job did not pass"),
        (phase("control", record()), phase("edited", record("edited", **GRADING_ERROR)),
         "FINDING", "edited: did not fire"),
        # A leak is judged first: beside a void, beside a record of another run, and before
        # a runtime failure.
        (phase("control", VOID_CONTROL, (RUN_SHOWS, ANSWERS), leaked=True), None, "FINDING",
         f"control: {LEAK}"),
        (phase("control", record()), phase("edited", record("edited", commit="fedcba9"),
         (ANSWERS,), leaked=True), "FINDING", f"edited: {LEAK}"),
        (phase("control", record(verdict="FAIL_RUNTIME", clauses={"R5": 1}), (ANSWERS,),
         leaked=True), None, "FINDING", f"control: {LEAK}"),
    ],
    ids=[
        "pass", "control-failed", "control-test-wrong", "r8-quiet", "floor-missed", "r5-fired",
        "void-control", "void-edited", "no-record", "no-edited-record", "other-commit",
        "other-model", "other-phase", "other-runs", "edited-missing", "no-class-budget",
        "r1-edited", "kill-missed-reported", "takeovers-reported", "edited-test-wrong",
        "grading-error-control", "grading-error-edited", "leak-beside-void",
        "leak-beside-no-edited-record", "leak-beside-runtime",
    ],
)  # fmt: skip
def test_each_case_of_the_judge(control: Any, edited_phase: Any, verdict: str, reason: str) -> None:
    found, reasons = judged(control, edited_phase)
    assert found == verdict, reasons
    assert any(reason in line for line in reasons), reasons


@pytest.mark.parametrize("clause", ["R2", "R5", "R6", "crash", "runtime"])
@pytest.mark.parametrize("where", ["control", "edited"])
def test_a_runtime_failure_in_either_job_is_a_finding_even_beside_a_void(
    clause: str, where: str
) -> None:
    void = {"void_clock": 1}
    if where == "control":
        control = record(verdict="FAIL_RUNTIME", clauses={clause: 1, **void}, voids=["clock"])
        found, reasons = judged(phase("control", control))
    else:
        found, reasons = judged(
            phase("control", record()),
            phase("edited", edited(**{clause: 1}, **void) | {"verdict": "FAIL_RUNTIME"}),
        )
    assert found == "FINDING", reasons
    assert f"{where}: the runtime failed the job: {clause}" in reasons


def test_a_crash_the_grade_could_not_count_is_a_finding_too() -> None:
    """A job never graded, after a harness error, has no clause of its own, and its record
    still says a worker crashed."""
    control = record(
        verdict="VOID",
        clauses={"void_harness_error": 1},
        voids=["harness_error"],
        floors={},
        crashed=True,
    )
    found, reasons = judged(phase("control", control, (RUN_SHOWS,)))
    assert found == "FINDING", reasons
    assert "control: the runtime failed the job: crash" in reasons


def test_in_the_control_every_test_passes_and_in_the_edited_job_three_fail(
    tmp_path: Path,
) -> None:
    """`judge` of each phase on the same junit, where every test passed."""
    nodes = [e.node for e in switched_off.phase_entry(LONG, "control").expect]
    cases = "".join(
        f'<testcase classname="tests.long_job.test_long_job_run" name="{n.split("::")[1]}"/>'
        for n in nodes
    )
    junit = tmp_path / "junit.xml"
    junit.write_text(f"<testsuites><testsuite>{cases}</testsuite></testsuites>")
    found = switched_off.outcomes(junit)
    control = switched_off.judge(switched_off.phase_entry(LONG, "control"), found)
    edited_lines = switched_off.judge(switched_off.phase_entry(LONG, "edited"), found)

    assert [line.split(":")[0] for line in control] == ["ok"] * 6
    assert [line.split(":")[0] for line in edited_lines] == [*["WRONG"] * 3, "ok", "ok", "ok"]
    assert nodes[-1] == switched_off.LONG_JOB_ANSWERS


def test_the_control_half_stops_after_the_control() -> None:
    assert judged(phase("control", record()), control_only=True)[0] == "PASS"
    assert judged(phase("control", record(verdict="INCONCLUSIVE")), control_only=True)[0] == (
        "FINDING"
    )
    # A run asks the control half alone first: a leak beside a void is judged there too.
    leaked = phase("control", VOID_CONTROL, (RUN_SHOWS, ANSWERS), leaked=True)
    assert judged(leaked, control_only=True)[0] == "FINDING"


def test_a_leak_lists_beside_it_what_else_the_jobs_show() -> None:
    runtime = phase(
        "control", record(verdict="FAIL_RUNTIME", clauses={"R5": 1}), (ANSWERS,), leaked=True
    )
    _, reasons = judged(runtime)
    assert "control: the runtime failed the job: R5" in reasons
    other_run = phase("edited", record("edited", commit="fedcba9"), (ANSWERS,), leaked=True)
    _, reasons = judged(phase("control", record()), other_run)
    assert any(line.startswith("edited: the record's commit is 'fedcba9'") for line in reasons)


def test_the_judge_prints_counts_and_fixed_names_only() -> None:
    found = edited() | {"note": ["a title", OTHER_SEED]}
    _, reasons = judged(phase("control", record()), phase("edited", found))
    assert reasons[1] == "edited: verdict FAIL, clauses R7 3, R8 1, R9 2"
    assert OTHER_SEED not in "\n".join(reasons)


def test_a_short_traceback_of_the_failing_tests_reads_ok_to_the_judge(tmp_path: Path) -> None:
    """The five tests on a record that fires R7, R8 and R9, as `run` runs them: their junit
    through `outcomes` and `judge` reads ok, and the output holds no id, code or title."""
    title = "GR-0042: access to remove, REF-7Q2-XK4"
    found = {
        "clauses": {"R7": 3, "R8": 1, "R9": 2},
        "floors": {"made": 7},
        "provider": "Claude API",
        "note": [title],
    }
    (tmp_path / "sample_record.py").write_text(
        "import json\n\nimport pytest\n\n\n@pytest.fixture\n"
        "def record() -> dict[str, object]:\n"
        f"    return json.loads({json.dumps(json.dumps(found))})\n"
    )
    junit = tmp_path / "junit.xml"
    done = subprocess.run(  # noqa: S603 - the test's own command
        [
            sys.executable,
            "-m",
            "pytest",
            *switched_off.LONG_JOB_PYTEST,
            "--noconftest",
            "-p",
            "sample_record",
            "-p",
            "no:cacheprovider",
            f"--junitxml={junit}",
            *(expect.node for expect in LONG.expect),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={
            **{k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")},
            "PYTHONPATH": str(tmp_path),
        },
        timeout=120,
    )
    judged_lines = switched_off.judge(LONG, switched_off.outcomes(junit))

    assert "--tb=short" in switched_off.LONG_JOB_PYTEST
    assert [line.split(":")[0] for line in judged_lines] == ["ok"] * 5, judged_lines
    output = done.stdout + done.stderr
    assert [text for text in (title, "GR-0042", "REF-7Q2-XK4") if text in output] == []


# The run: the control first, the edit only after it passed


def stub_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    outcomes: Mapping[str, dict[str, Any]],
) -> list[tuple[str, Any]]:
    """`run` with its git, its stack and its phases recorded; the checkout is `tmp_path`."""
    rules = long_rules(LONG_KEYED_TOKEN_BOUND=8_000_000, refusals=lambda env, root: [])
    monkeypatch.setattr(switched_off, "long_job_rules", lambda root=ROOT: rules)
    monkeypatch.setattr(switched_off, "ROOT", tmp_path)
    agent = tmp_path / switched_off.AGENT
    agent.parent.mkdir(parents=True)
    agent.write_text((ROOT / switched_off.AGENT).read_text())
    happened: list[tuple[str, Any]] = []

    def run_phase(entry: Any, provider: str, name: str) -> Any:
        happened.append(
            ("phase", (name, agent.read_text() != (ROOT / switched_off.AGENT).read_text()))
        )
        return phase(name, outcomes.get(name))

    def sh(*args: str, env: Any) -> None:
        happened.append(("sh", list(args)))

    monkeypatch.setattr(switched_off, "run_phase", run_phase)
    monkeypatch.setattr(switched_off, "_sh", sh)
    monkeypatch.setattr(switched_off, "_git", lambda *args: "" if args[0] == "status" else HEAD)
    monkeypatch.setenv("SWITCHED_OFF_PROVIDER", "claude")
    return happened


def test_a_failed_control_writes_no_edit_and_is_a_finding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    happened = stub_run(monkeypatch, tmp_path, {"control": record(verdict="FAIL")})
    before = (tmp_path / switched_off.AGENT).read_text()

    assert switched_off.run(NAME) == 1
    assert happened == [("phase", ("control", False))]
    assert (tmp_path / switched_off.AGENT).read_text() == before
    assert capsys.readouterr().out.splitlines()[-1] == f"FINDING: {NAME}"


def test_an_edit_that_matches_no_line_stops_the_run_before_the_control(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A miss found only after the control would have spent a job on the Claude API."""
    happened = stub_run(monkeypatch, tmp_path, {"control": record(), "edited": edited()})
    agent = tmp_path / switched_off.AGENT
    (edit,) = LONG.edits
    agent.write_text(agent.read_text().replace(edit.old, ""))

    assert switched_off.run(NAME) == 2
    assert happened == []
    assert capsys.readouterr().err.startswith("not started: ")


def test_a_passed_control_is_followed_by_the_edit_and_the_edited_job(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    happened = stub_run(monkeypatch, tmp_path, {"control": record(), "edited": edited()})

    assert switched_off.run(NAME) == 0
    assert [kind for kind, _ in happened] == ["phase", "sh", "phase"]
    assert happened[0] == ("phase", ("control", False))
    assert happened[1][1][:3] == ["git", "--no-pager", "diff"]
    assert happened[2] == ("phase", ("edited", True))
    assert capsys.readouterr().out.splitlines()[-1] == f"PASS: {NAME}"


def test_a_phase_starts_its_stack_and_runs_the_tests_with_short_tracebacks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The edited phase on the Claude API, every command recorded and none run."""
    commands: list[tuple[list[str], dict[str, str]]] = []
    monkeypatch.setattr(switched_off, "ROOT", tmp_path)
    monkeypatch.setattr(
        switched_off, "_sh", lambda *args, env: commands.append((list(args), dict(env)))
    )

    def pytest_run(args: list[str], **more: Any) -> Any:
        commands.append((list(args), dict(more["env"])))
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(switched_off.subprocess, "run", pytest_run)
    monkeypatch.setenv("LONG_JOB_SEED", OTHER_SEED)
    monkeypatch.setenv("CLAUDE_API_KEY", NOT_A_KEY)
    found = switched_off.run_phase(LONG, "claude", "edited")

    (stack, _), (tests, tests_env) = commands
    assert stack == switched_off.stack_commands("long", "claude")[0]
    assert "docker-compose.claude.yml" in stack
    assert tests[:6] == ["uv", "run", "--frozen", "pytest", "-v", "--tb=short"]
    assert "-s" not in tests
    assert "--junitxml=.switched-off/junit-edited.xml" in tests
    assert tests[-6:] == [*(e.node for e in LONG.expect), switched_off.LONG_JOB_ANSWERS]
    assert tests_env["LONG_JOB_PHASE"] == "edited"
    assert (tests_env["LONG_JOB_SEED"], tests_env["CLAUDE_API_KEY"]) == (OTHER_SEED, NOT_A_KEY)
    assert "LONG_JOB_TOKEN_BUDGET" not in tests_env
    assert found.lines == ("WRONG: pytest wrote no results",)


FOUND_FAILURE = f'<failure message="AssertionError: {switched_off.ANSWER_FOUND}">x</failure>'
# Each case's children of the answers test's junit cases, one tuple item per case: pytest
# writes a second case for a test whose teardown failed after the test itself.
ANSWERS_FAILURES = {
    "found": (FOUND_FAILURE,),
    "nothing-to-look-in": ('<failure message="AssertionError: nothing to look in">x</failure>',),
    "no-records-file": ('<failure message="FileNotFoundError: results.jsonl">x</failure>',),
    "fixture-error": (f'<error message="{switched_off.ANSWER_FOUND}">x</error>',),
    "teardown-error-after-leak": (
        FOUND_FAILURE,
        '<error message="failed on teardown with &quot;RuntimeError&quot;">x</error>',
    ),
    "passed": ("",),
}


@pytest.mark.parametrize("case", ANSWERS_FAILURES)
def test_a_phase_is_leaked_only_when_the_answers_test_failed_on_a_found_answer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str
) -> None:
    """The answers test's junit as `run_phase` reads it; no stack, no job."""
    monkeypatch.setattr(switched_off, "ROOT", tmp_path)
    monkeypatch.setattr(switched_off, "_sh", lambda *args, env: None)
    test = switched_off.LONG_JOB_ANSWERS.split("::")[1]
    case_xml = "".join(
        f'<testcase classname="tests.long_job.test_long_job_run" name="{test}">{child}</testcase>'
        for child in ANSWERS_FAILURES[case]
    )

    def pytest_run(args: list[str], **more: Any) -> Any:
        (junit,) = [arg.split("=", 1)[1] for arg in args if arg.startswith("--junitxml=")]
        (tmp_path / junit).write_text(f"<testsuites><testsuite>{case_xml}</testsuite></testsuites>")
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(switched_off.subprocess, "run", pytest_run)
    found = switched_off.run_phase(LONG, "fake", "control")

    assert found.leaked is (case in ("found", "teardown-error-after-leak"))


def answers_junit(tmp_path: Path, name: str, child: str) -> Path:
    junit = tmp_path / "junit.xml"
    case = (
        f'<testcase classname="tests.long_job.test_long_job_run" name="{name}">{child}</testcase>'
    )
    junit.write_text(f"<testsuites><testsuite>{case}</testsuite></testsuites>")
    return junit


def test_a_found_answer_counts_only_in_the_answers_test_and_also_in_its_text(
    tmp_path: Path,
) -> None:
    answers = switched_off.LONG_JOB_ANSWERS.split("::")[1]
    in_text = f'<failure message="AssertionError">{switched_off.ANSWER_FOUND}</failure>'
    assert switched_off.answers_leaked(answers_junit(tmp_path, answers, in_text)) is True
    other = answers_junit(tmp_path, "test_the_log_holds", FOUND_FAILURE)
    assert switched_off.answers_leaked(other) is False
    assert switched_off.answers_leaked(tmp_path / "missing.xml") is False


# The rehearsal: the control only, on the fake provider, and the bound not asked


def test_a_rehearsal_starts_with_the_bound_not_set_and_makes_no_edit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rules = long_rules(LONG_KEYED_TOKEN_BOUND=None)
    monkeypatch.setattr(switched_off, "long_job_rules", lambda root=ROOT: rules)
    calls: list[tuple[list[str], dict[str, str]]] = []

    def pytest_run(args: list[str], **more: Any) -> Any:
        """The harness's session, as it leaves its junit and its record."""
        calls.append((list(args), dict(more["env"])))
        junit = tmp_path / ".switched-off" / "junit-control.xml"
        cases = "".join(
            f'<testcase classname="tests.long_job.test_long_job_run" name="{node.split("::")[1]}"/>'
            for node in args
            if "::" in node
        )
        junit.write_text(f"<testsuites><testsuite>{cases}</testsuite></testsuites>")
        session = tmp_path / ".long-job" / "2026-11-26T120000Z"
        session.mkdir(parents=True)
        fake = record(provider="fake provider", model="fake-long-job", seed="committed")
        (session / "results.jsonl").write_text(json.dumps(fake) + "\n")
        return SimpleNamespace(returncode=0)

    def edited_files(*args: Any) -> Any:
        raise AssertionError("a rehearsal makes no edit")

    monkeypatch.setattr(switched_off, "ROOT", tmp_path)
    monkeypatch.setattr(switched_off, "_git", lambda *args: "" if args[0] == "status" else HEAD)
    monkeypatch.setattr(
        switched_off, "_sh", lambda *args, env: calls.append((list(args), dict(env)))
    )
    monkeypatch.setattr(switched_off.subprocess, "run", pytest_run)
    monkeypatch.setattr(switched_off, "edited_files", edited_files)
    monkeypatch.delenv("SWITCHED_OFF_PROVIDER", raising=False)
    for key, value in INHERITED.items():
        if key != "SWITCHED_OFF_PROVIDER":
            monkeypatch.setenv(key, value)

    assert switched_off.run(NAME, rehearsal=True) == 0
    printed = capsys.readouterr().out
    assert printed.splitlines()[-2:] == ["control: PASS", f"REHEARSAL OK: {NAME}"]
    assert calls[0][0] == ["make", "long-up"]
    for _, env in calls:
        assert not [k for k in env if k in ("LONG_JOB_SEED", "CLAUDE_API_KEY")]
        assert not [k for k in env if k.startswith("ANTHROPIC_")]
        assert "LONG_JOB_TOKEN_BUDGET" not in env
        assert env["LONG_JOB_PROVIDER"] == "fake"
    assert OTHER_SEED not in printed


# The edit, run through replay


def summary_step(payload: dict[str, Any]) -> Step:
    when = datetime(2026, 11, 26, tzinfo=UTC)
    return Step(UUID(int=1), 1, StepKind.SUMMARY, payload, 1, when)


def edited_payload(summaries: int) -> dict[str, Any]:
    """The summary's payload after the entry's own lines ran, as the worker builds it."""
    (edit,) = LONG.edits
    content = [{"type": "text", "text": "Notices: GR-0042 left. Read so far: 3 files."}]
    names: dict[str, Any] = {
        "summaries": summaries,
        "payload": {"content": copy.deepcopy(content), "stop_reason": "end_turn"},
        "response": SimpleNamespace(content=content),
        "opening_message": opening_message,
        "summary_text": summary_text,
    }
    exec(textwrap.dedent(edit.new), names)  # noqa: S102 - the list's own text
    payload: dict[str, Any] = names["payload"]
    return payload


def test_the_edited_first_summary_passes_replays_message_check() -> None:
    """So the takeover of the first kill does not fail the edited job on its message."""
    payload = edited_payload(0)
    assert payload["content"] == [{"type": "text", "text": switched_off.NOTICES_DROPPED}]
    assert payload["message"] == opening_message(switched_off.NOTICES_DROPPED)
    state = replay("Review the access records.", [summary_step(payload)])
    assert (state.problem, state.messages) == (None, [payload["message"]])

    # The message built from the model's own text, beside the changed text: refused.
    original = [{"type": "text", "text": "Notices: GR-0042 left. Read so far: 3 files."}]
    mismatched = {**payload, "message": opening_message(summary_text(original))}
    state = replay("Review the access records.", [summary_step(mismatched)])
    assert state.problem == "its message does not match its text"
    rules = switched_off.long_job_rules()
    assert rules.error_class(f"the summary is not usable: {state.problem}") == "message_mismatch"


def test_a_later_summary_is_left_as_the_model_wrote_it() -> None:
    payload = edited_payload(1)
    assert "GR-0042" in payload["content"][0]["text"]
    assert payload["message"] == opening_message(summary_text(payload["content"]))


# The workflow job


def job(name: str) -> str:
    lines = WORKFLOW_FILE.read_text().splitlines()
    start = lines.index(f"  {name}:")
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i][:2] == "  " and lines[i][2:3].strip()),
        len(lines),
    )
    return "\n".join(lines[start:end])


def steps(name: str) -> list[str]:
    return ("\n" + job(name).split("    steps:\n", 1)[1]).split("\n      - ")[1:]


def index(found: list[str], text: str) -> int:
    (at,) = [i for i, step in enumerate(found) if text in step]
    return at


def run_lines(name: str) -> list[str]:
    """The shell lines of a job's steps: each `run:` line and the block under it."""
    found, block = [], False
    for line in job(name).splitlines():
        if line.strip().startswith("run:"):
            found.append(line)
            block = line.strip() in {f"run: {style}" for style in ("|", "|-", ">", ">-")}
        elif block and line.startswith("          "):
            found.append(line)
        else:
            block = False
    return found


def test_the_long_job_job_starts_only_for_its_suite_on_the_claude_api_and_shows_its_mode() -> None:
    text = job("long-job")
    head = text.split("    steps:")[0]
    assert "    if: inputs.suite == 'long-job' && inputs.provider == 'claude'\n" in head
    assert "    name: long-job ${{ inputs.long_job }} on ${{ inputs.model }}\n" in head
    assert "    environment: claude-api\n" in head
    assert "      group: claude-api\n      cancel-in-progress: false\n" in head
    minutes = switched_off.long_job_rules().WORKFLOW_MINUTES
    assert f"    timeout-minutes: {minutes}\n" in head
    assert "secrets." not in head
    assert "long-job" in switched_off.options(WORKFLOW_FILE.read_text(), "suite")
    assert switched_off.options(WORKFLOW_FILE.read_text(), "long_job") == [
        "counts",
        "must-fail",
        "graded",
    ]


def test_the_key_reaches_the_long_job_job_only_after_its_refusals() -> None:
    found = steps("long-job")
    with_key = [i for i, step in enumerate(found) if "secrets.CLAUDE_API_KEY" in step]
    with_secret = [i for i, step in enumerate(found) if "secrets." in step]
    off_main = index(found, "name: Refuse a long-job run off main")
    model = index(found, "name: Check the model name")
    digits = index(found, "name: Check the numbers")
    entry = index(found, "dispatch long-notices-dropped claude")
    check = index(found, "scripts/long_job_keyed.py check")
    assert (off_main, model, digits) == (0, 1, 2)
    assert "if: github.ref != 'refs/heads/main'" in found[off_main]
    assert digits < min(with_secret)
    assert entry < check < min(with_key)
    # The check gets the seed, not the key.
    assert "secrets.LONG_JOB_SEED" in found[check]
    assert "secrets.CLAUDE_API_KEY" not in found[check]
    assert "LONG_JOB_PROVIDER: claude" in found[check]
    assert "if: inputs.long_job == 'must-fail'" in found[entry]
    run = index(found, "\n          make long-job-claude ")
    must_fail = index(found, "SWITCHED_OFF: long-notices-dropped")
    assert {run, must_fail} == set(with_key)
    assert "if: inputs.long_job != 'must-fail'" in found[run]
    assert "if: inputs.long_job == 'must-fail'" in found[must_fail]
    assert "SWITCHED_OFF_PROVIDER: claude" in found[must_fail]
    assert "\n          make switched-off\n" in found[must_fail]
    # No comment of the job names a secret: the count of the text is exact.
    comments = [line for line in job("long-job").splitlines() if line.strip().startswith("#")]
    assert not [line for line in comments if "secrets" in line]


def test_the_long_job_job_pastes_no_expression_into_a_shell_line() -> None:
    assert run_lines("long-job")
    assert not [line for line in run_lines("long-job") if "${{" in line]
    head = job("long-job").split("    steps:")[0]
    for name in ("token_budget", "jobs", "long_job", "model"):
        assert f"${{{{ inputs.{name} }}}}" in head


def test_the_long_job_job_prints_the_records_and_nothing_else_it_kept() -> None:
    text = job("long-job")
    assert "docker compose logs" not in text
    assert "logs/" not in text
    assert "upload-artifact" not in text
    assert "for file in .long-job/*/results.jsonl; do" in text
    assert "enable-cache: false" in text


def test_the_fake_job_runs_the_harness_on_the_fake_provider_and_shows_its_files() -> None:
    fake = job("fake")
    assert "      - name: Long job harness\n        if: inputs.suite == 'long-job'\n" in fake
    assert "\n          make long-job\n" in fake
    assert ".long-job/*/results.jsonl" in fake
    assert ".long-job/*/logs/*.log" in fake
