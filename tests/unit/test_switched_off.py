"""The switched-off list stays true to the code; its judge counts only the named failure.

A switched-off run proves a test can fail only if the edit still lands and
the test fails on the assertion that guards the mechanism: not on a setup
error, not on another assertion, and not by never running.

An entry on the Claude API gets the key and costs money. So what starts it,
what its run may spend, what its edit does and how its record is judged are
all checked here, on made-up records, before any such run.
"""

import copy
import importlib.util
import json
import subprocess
import sys
import textwrap
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from runtime.store.steps import Step, StepKind
from runtime.worker.replay import replay

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "switched_off.py"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("switched_off", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses look the module up while the class body runs.
    sys.modules["switched_off"] = module
    spec.loader.exec_module(module)
    return module


switched_off = load()


def test_every_edit_matches_once_and_still_compiles_and_every_test_exists() -> None:
    assert switched_off.check() == []


WORKFLOW = (ROOT / ".github/workflows/manual-runs.yml").read_text()
options = switched_off.options


def test_the_workflow_offers_exactly_the_listed_runs() -> None:
    assert options(WORKFLOW, "switched_off") == list(switched_off.ENTRIES)
    assert "switched-off" in options(WORKFLOW, "suite")
    assert "switched-off-rehearsal" in options(WORKFLOW, "suite")


def test_an_edit_whose_text_is_not_unique_is_refused(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\nx = 1\n")
    with pytest.raises(ValueError, match="2 times"):
        switched_off.edited(switched_off.Edit("a.py", "x = 1\n", "x = 2\n"), tmp_path)


NODE = "tests/unit/test_x.py::test_y"
ENTRY = switched_off.SwitchedOff(
    issue=1,
    what="w",
    edits=(),
    stack="up",
    expect=(switched_off.Expect(NODE, "total <= BUDGET"),),
)


def junit(tmp_path: Path, case: str) -> Path:
    path = tmp_path / "junit.xml"
    path.write_text(
        '<testsuites><testsuite name="pytest">'
        f'<testcase classname="tests.unit.test_x" name="test_y">{case}</testcase>'
        "</testsuite></testsuites>"
    )
    return path


@pytest.mark.parametrize(
    ("case", "verdict"),
    [
        ('<failure message="AssertionError: assert 9 &lt;= 5">&gt;   assert total &lt;= BUDGET'
         "</failure>", "ok"),
        ('<failure message="AssertionError">assert job.status == "failed"</failure>', "WRONG"),
        # The named assertion only as context: pytest prints the source above
        # the failing line, which it marks with ">".
        ('<failure message="AssertionError: assert 1 == 2">'
         "    assert total &lt;= BUDGET\n&gt;   assert job.status == 2</failure>", "WRONG"),
        ('<error message="fixture failed">stack never came up</error>', "WRONG"),
        ('<skipped message="no stack" />', "WRONG"),
        ("", "WRONG"),
    ],
    ids=["named-failure", "other-assertion", "named-only-as-context", "error", "skipped", "passed"],
)  # fmt: skip
def test_only_a_failure_on_the_named_assertion_counts(
    tmp_path: Path, case: str, verdict: str
) -> None:
    lines = switched_off.judge(ENTRY, switched_off.outcomes(junit(tmp_path, case)))
    assert [line.split(":")[0] for line in lines] == [verdict]


def test_a_test_that_did_not_run_is_wrong(tmp_path: Path) -> None:
    other = junit(tmp_path, "").read_text().replace("test_y", "test_z")
    (tmp_path / "junit.xml").write_text(other)
    lines = switched_off.judge(ENTRY, switched_off.outcomes(tmp_path / "junit.xml"))
    assert lines == [f"WRONG: {NODE}: not run, expected failed on 'total <= BUDGET'"]


def test_a_test_expected_to_pass_must_pass(tmp_path: Path) -> None:
    entry = switched_off.SwitchedOff(
        issue=1, what="w", edits=(), stack="up", expect=(switched_off.Expect(NODE, None, "r"),)
    )
    passed = switched_off.judge(entry, switched_off.outcomes(junit(tmp_path, "")))
    failed = switched_off.judge(
        entry, switched_off.outcomes(junit(tmp_path, '<failure message="x">x</failure>'))
    )
    assert passed[0].startswith("ok") and failed[0].startswith("WRONG")


def test_a_name_outside_the_list_is_refused_before_anything_changes() -> None:
    done = subprocess.run(  # noqa: S603 - fixed command
        [sys.executable, str(SCRIPT), "run", "rm -rf /"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert done.returncode == 2
    assert "not a switched-off run" in done.stderr


# An entry on the Claude API

NAME = "thinking-signature-changed"
KEYED = switched_off.ENTRIES[NAME]
SENT, ANSWERED, REFUSED = switched_off.SENT, switched_off.ANSWERED, switched_off.REFUSED


def test_the_one_entry_on_the_claude_api_is_bounded() -> None:
    keyed = {name: e for name, e in switched_off.ENTRIES.items() if e.provider == "claude"}
    assert list(keyed) == [NAME]
    assert (KEYED.model, KEYED.stack, KEYED.runs) == ("claude-fable-5-2", "kills", 5)
    assert KEYED.runs * switched_off.kill_rules().TOKEN_BUDGET == switched_off.KEYED_TOKEN_BOUND
    assert KEYED.record == switched_off.Refusals(test="failpoint after_tool_ran", at_least=3)


IN_THE_CALL = KEYED.expect[0]
RANDOM_KILLS = replace(
    IN_THE_CALL, node="tests/kills/test_kill_runs.py::test_jobs_survive_a_kill_at_a_random_time"
)


@pytest.mark.parametrize(
    ("changed", "problem"),
    [
        (replace(KEYED, model="claude-sonnet-5-5"), "the workflow offers no model"),
        (replace(KEYED, model=None), "the workflow offers no model None"),
        (replace(KEYED, stack="chaos"), "a run on the Claude API uses the kills stack"),
        (replace(KEYED, record=None), "is judged by its record"),
        (replace(KEYED, provider="fake"), "the fake provider takes no model"),
        (replace(KEYED, runs=6), "6 jobs may spend 300000 tokens, over 250000"),
        (replace(KEYED, runs=0), "`runs` is 0, not 1 to 40"),
        (replace(KEYED, runs=41), "`runs` is 41, not 1 to 40"),
        (
            replace(KEYED, expect=(replace(IN_THE_CALL, node="tests/chaos/test_kills.py::x"),)),
            "the kills stack runs tests/kills only",
        ),
        (replace(KEYED, expect=(RANDOM_KILLS,)), "is not the one test's"),
        (replace(KEYED, expect=(IN_THE_CALL, RANDOM_KILLS)), "is not the one test's"),
        (
            replace(KEYED, record=switched_off.Refusals("failpoint after_tool_intent", 3)),
            "is not the one test's",
        ),
        (replace(KEYED, record=switched_off.Refusals("random kills", 3)), "is not the one test's"),
        (
            replace(KEYED, record=switched_off.Refusals("failpoint after_tool_ran", 6)),
            "`at_least` is 6, not 1 to 5",
        ),
        (
            replace(KEYED, record=switched_off.Refusals("failpoint after_tool_ran", 0)),
            "`at_least` is 0, not 1 to 5",
        ),
        (
            replace(switched_off.ENTRIES["budget-off"], runs=5),
            "`runs` and `record` are for the kills stack",
        ),
    ],
)
def test_an_entry_that_may_not_get_the_key_is_refused_by_the_check(
    changed: Any, problem: str
) -> None:
    assert switched_off.run_problems(KEYED) == []
    assert any(problem in found for found in switched_off.run_problems(changed))


def test_the_check_needs_the_counts_the_judge_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """A count renamed in the kill runs would otherwise be missed in the run that costs money."""
    rules = switched_off.kill_rules()
    monkeypatch.setattr(rules, "thinking_record", lambda runs: {SENT: 0, ANSWERED: 0})
    monkeypatch.setattr(switched_off, "kill_rules", lambda root=ROOT: rules)
    assert any(REFUSED in found for found in switched_off.run_problems(KEYED))


# The edit of the entry

THINKING = {"type": "thinking", "thinking": "", "signature": "EuYBCkYIBxgC"}
SECOND = {"type": "thinking", "thinking": "", "signature": "Aq8xEuYB"}
UNTOUCHED = [
    {"type": "thinking", "thinking": ""},
    {"type": "thinking", "thinking": "", "signature": ""},
    {"type": "thinking", "thinking": "", "signature": None},
    {"type": "redacted_thinking", "data": "EuYB"},
    {"type": "text", "text": "Done.", "signature": "EuYB"},
    {"type": "tool_use", "id": "toolu_2", "name": "create_issue", "input": {"signature": "EuYB"}},
]


def stored_steps() -> list[Step]:
    def step(seq: int, kind: StepKind, payload: dict[str, Any]) -> Step:
        return Step(UUID(int=1), seq, kind, payload, 1, datetime(2026, 11, 21, tzinfo=UTC))

    read = {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "ci.log"}}
    return [
        step(1, StepKind.MODEL_RESPONSE, {"content": [THINKING, read], "stop_reason": "tool_use"}),
        step(
            2,
            StepKind.TOOL_RESULT,
            {"tool_use_id": "toolu_1", "content": "1 failed", "is_error": False},
        ),
        step(
            3,
            StepKind.MODEL_RESPONSE,
            {"content": [SECOND, *UNTOUCHED], "stop_reason": "tool_use"},
        ),
    ]


def test_the_edit_changes_the_first_character_of_each_signature_and_nothing_else() -> None:
    """The lines the entry puts into the worker, run on a conversation rebuilt from steps.

    An edit that changed nothing would let every job pass, and the run
    would report that the API answered a changed block.
    """
    (edit,) = KEYED.edits
    assert edit.new.startswith(edit.old)
    names: dict[str, Any] = {
        "replay": replay,
        "spec": SimpleNamespace(prompt="Read ci.log."),
        "steps": copy.deepcopy(stored_steps()),
    }
    exec(textwrap.dedent(edit.new), names)  # noqa: S102 - the list's own text
    changed = names["state"].messages
    plain = replay("Read ci.log.", copy.deepcopy(stored_steps())).messages
    assert changed[1]["content"][0]["signature"] == "AuYBCkYIBxgC"
    # A signature that starts with the character the others are given gets another.
    assert changed[3]["content"][0]["signature"] == "Bq8xEuYB"
    assert changed[3]["content"][1:] == UNTOUCHED
    changed[1]["content"][0]["signature"] = THINKING["signature"]
    changed[3]["content"][0]["signature"] = SECOND["signature"]
    assert changed == plain


# What starts a run


def test_an_entry_starts_only_on_its_own_provider_and_model() -> None:
    dispatch = switched_off.dispatch_problem
    assert dispatch(NAME, "claude", "claude-fable-5-2") is None
    assert dispatch(NAME, "claude", "claude-haiku-5-5") == (
        f"{NAME} runs on model claude-fable-5-2, not claude-haiku-5-5"
    )
    assert dispatch(NAME, "claude", None) == f"{NAME} runs on model claude-fable-5-2, not None"
    assert dispatch(NAME, "fake", None) == f"{NAME} runs on provider claude, not fake"
    assert dispatch("budget-off", "fake", None) is None
    # The model input is ignored on the fake provider.
    assert dispatch("budget-off", "fake", "claude-fable-5-2") is None
    assert dispatch("budget-off", "claude", "claude-fable-5-2") == (
        "budget-off runs on provider fake, not claude"
    )


@pytest.mark.parametrize(
    ("arguments", "status"),
    [
        (["dispatch", NAME, "claude", "claude-fable-5-2"], 0),
        (["dispatch", NAME, "claude", "claude-opus-5-5"], 1),
        (["dispatch", NAME, "fake"], 1),
        (["dispatch", "budget-off", "fake"], 0),
        (["dispatch", "budget-off", "claude", "claude-fable-5-2"], 1),
        (["dispatch", "no-such-run", "fake"], 2),
        (["rehearse", "no-such-run"], 2),
    ],
)
def test_the_dispatch_check_fails_a_start_that_is_not_the_entrys(
    arguments: list[str], status: int
) -> None:
    assert switched_off.main(arguments) == status


def test_a_run_on_the_claude_api_needs_to_be_asked_for() -> None:
    start = switched_off.start_problem
    assert start(NAME, "claude", rehearsal=False) is None
    assert "set SWITCHED_OFF_PROVIDER=claude" in (start(NAME, "fake", rehearsal=False) or "")
    assert "set SWITCHED_OFF_PROVIDER=claude" in (start(NAME, "Claude", rehearsal=False) or "")
    assert start("budget-off", "fake", rehearsal=False) is None
    assert "runs on the fake provider" in (start("budget-off", "claude", rehearsal=False) or "")


def test_only_an_entry_of_the_claude_api_is_rehearsed_and_never_with_the_key_asked_for() -> None:
    start = switched_off.start_problem
    assert start(NAME, "fake", rehearsal=True) is None
    assert "unset SWITCHED_OFF_PROVIDER" in (start(NAME, "claude", rehearsal=True) or "")
    assert "nothing to rehearse" in (start("budget-off", "fake", rehearsal=True) or "")


@pytest.mark.parametrize(
    ("name", "asked", "rehearsal"),
    [(NAME, None, False), (NAME, "claude", True), ("budget-off", "claude", False)],
)
def test_a_refused_start_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, name: str, asked: str | None, rehearsal: bool
) -> None:
    """The refusal comes before the checkout is read or edited and before any stack starts."""

    def reached(*args: str, **more: Any) -> str:
        raise AssertionError(f"the run went on: {args}")

    monkeypatch.setattr(switched_off, "_git", reached)
    monkeypatch.setattr(switched_off, "_sh", reached)
    monkeypatch.setattr(switched_off, "edited", reached)
    monkeypatch.delenv("SWITCHED_OFF_PROVIDER", raising=False)
    if asked is not None:
        monkeypatch.setenv("SWITCHED_OFF_PROVIDER", asked)
    assert switched_off.run(name, rehearsal=rehearsal) == 2


def test_an_entry_over_its_bound_is_refused_at_the_start_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A list that CI did not check still cannot start a run past the bound."""
    over = replace(KEYED, runs=6)
    monkeypatch.setitem(switched_off.ENTRIES, NAME, over)

    def reached(*args: str, **more: Any) -> str:
        raise AssertionError(f"the run went on: {args}")

    monkeypatch.setattr(switched_off, "_git", reached)
    monkeypatch.setattr(switched_off, "_sh", reached)
    monkeypatch.setattr(switched_off, "edited", reached)
    monkeypatch.setenv("SWITCHED_OFF_PROVIDER", "claude")
    assert switched_off.run(NAME) == 2
    assert switched_off.main(["dispatch", NAME, "claude", "claude-fable-5-2"]) == 1
    monkeypatch.delenv("SWITCHED_OFF_PROVIDER")
    assert switched_off.run(NAME, rehearsal=True) == 2


# The environment of a run

INHERITED = {
    "PATH": "/usr/bin",
    "DOCKER_GID": "999",
    "CLAUDE_API_KEY": "sk-ant-not-a-key",
    "ANTHROPIC_API_KEY": "sk-ant-not-a-key-either",
    "SWITCHED_OFF_PROVIDER": "claude",
    "KILLS_PROVIDER": "claude",
    "KILLS_MODEL": "claude-opus-5-5",
    "KILLS_RUNS": "20",
    "KILLS_FAILPOINT_RUNS": "9",
    "KILLS_JOB_LIMIT": "40",
    "KILLS_WINDOW": "7.82",
    "KILLS_SEED": "1",
    "FAILPOINT": "after_claim",
}


def test_a_run_on_the_claude_api_gets_its_settings_from_the_entry_alone() -> None:
    env = switched_off.child_environment(KEYED, "claude", INHERITED)
    assert env == {
        "PATH": "/usr/bin",
        "DOCKER_GID": "999",
        "CLAUDE_API_KEY": "sk-ant-not-a-key",
        "KILLS_PROVIDER": "claude",
        "KILLS_MODEL": "claude-fable-5-2",
        "KILLS_RUNS": "5",
        "KILLS_FAILPOINT_RUNS": "5",
        "KILLS_JOB_LIMIT": "5",
    }
    # The kill runner takes these settings, and refuses a sixth job.
    chosen = switched_off.kill_rules().settings(env)
    assert (chosen.provider, chosen.model, chosen.failpoint_runs) == (
        "claude",
        "claude-fable-5-2",
        5,
    )
    assert chosen.refusal(5) is not None


def test_a_rehearsal_and_a_run_on_the_fake_provider_get_no_key() -> None:
    env = switched_off.child_environment(KEYED, "fake", INHERITED)
    assert env == {
        "PATH": "/usr/bin",
        "DOCKER_GID": "999",
        "KILLS_PROVIDER": "fake",
        "KILLS_RUNS": "5",
        "KILLS_FAILPOINT_RUNS": "5",
        "KILLS_JOB_LIMIT": "5",
    }
    chosen = switched_off.kill_rules().settings(env)
    assert (chosen.provider, chosen.refusal(5) is not None) == ("fake", True)
    other = switched_off.child_environment(switched_off.ENTRIES["budget-off"], "fake", INHERITED)
    assert other == {"PATH": "/usr/bin", "DOCKER_GID": "999"}


def test_the_scripts_own_git_commands_never_get_the_key() -> None:
    # The kill runner's own git and docker commands do get it: they run in the tests.
    assert switched_off.without_keys(INHERITED).keys() == INHERITED.keys() - {
        "CLAUDE_API_KEY",
        "ANTHROPIC_API_KEY",
    }


# The record of a run

HEAD = "0123456789abcdef0123456789abcdef01234567"
REFUSAL = (
    "model call failed: provider answered 400: messages.3.content.0:"
    " Invalid `signature` in `thinking` block."
)
AS_LISTED = [f"ok: {IN_THE_CALL.node}: failed, expected failed on 'assert not failures'"]


def record(**changes: Any) -> dict[str, Any]:
    """The record of a run in which four of five jobs sent a changed block and were refused."""
    return {
        "test": "failpoint after_tool_ran",
        "commit": "0123456 (uncommitted changes)",
        "provider": "Claude API",
        "model": "claude-fable-5-2",
        "runs": 5,
        "failed_runs": 4,
        "problems": 4,
        SENT: 4,
        ANSWERED: 0,
        REFUSED: 4,
        "errors_of_failed_jobs": [REFUSAL],
        "verdict": "failed",
        **changes,
    }


def judged(
    found: dict[str, Any], tests: list[str] | None = None, trouble: list[str] | None = None
) -> tuple[str, list[str]]:
    verdict, reasons = switched_off.judge_record(
        KEYED,
        AS_LISTED if tests is None else tests,
        [] if trouble else [found],
        trouble or [],
        head=HEAD,
        rules=switched_off.kill_rules(),
    )
    return verdict, reasons


def few(refused: int) -> dict[str, Any]:
    return record(**{SENT: refused, REFUSED: refused, "failed_runs": refused, "problems": refused})


@pytest.mark.parametrize(
    ("found", "verdict", "reason"),
    [
        (record(), "PASS", "every job that sent a changed block was refused"),
        (few(5), "PASS", "5 sent a rebuilt thinking block"),
        (few(3), "PASS", "3 of them were refused"),
        (few(2), "INCONCLUSIVE", "2 jobs sent a changed block, fewer than 3"),
        (
            {**few(0), "verdict": "passed", "errors_of_failed_jobs": []},
            "INCONCLUSIVE",
            "0 jobs sent a changed block, fewer than 3",
        ),
        (record(verdict="void", void="the wall clock moved +9.0 s"), "INCONCLUSIVE", "void"),
        # One job was answered and passed; three were refused.
        (
            {**few(3), SENT: 4, ANSWERED: 1},
            "FINDING",
            "the API answered 1 jobs that sent a changed block",
        ),
        # One job was answered and failed afterwards.
        (record(**{ANSWERED: 1, REFUSED: 3}), "FINDING", "the API answered 1 jobs"),
        (record(failed_runs=5), "FINDING", "a job failed another way"),
        # A refused job with a second problem, such as two takeovers.
        (record(problems=5), "FINDING", "a job failed another way"),
        (record(**{SENT: 5}), "FINDING", "a job failed another way"),
        (
            record(
                **{REFUSED: 0},
                errors_of_failed_jobs=[
                    REFUSAL + " The block is bound to a different conversation."
                ],
            ),
            "FINDING",
            "replay changed the conversation, not only the signature",
        ),
        (record(verdict="passed"), "FINDING", "the test did not end as the list says"),
        (record(model="claude-opus-5-5"), "INCONCLUSIVE", "the record's model"),
        (record(provider="fake provider"), "INCONCLUSIVE", "the record's provider"),
        (record(runs=20), "INCONCLUSIVE", "the record's runs is 20, not 5"),
        (record(commit="fedcba9 (uncommitted changes)"), "INCONCLUSIVE", "the record's commit"),
        (record(commit=""), "INCONCLUSIVE", "the record's commit"),
        (record(**{REFUSED: None}), "INCONCLUSIVE", f"no count {REFUSED}"),
        (record(**{ANSWERED: False}), "INCONCLUSIVE", f"no count {ANSWERED}"),
        (record(**{SENT: "4"}), "INCONCLUSIVE", f"no count {SENT}"),
    ],
)
def test_a_run_on_the_claude_api_is_judged_by_its_record(
    found: dict[str, Any], verdict: str, reason: str
) -> None:
    got, reasons = judged(found)
    assert got == verdict, reasons
    assert any(reason in line for line in reasons), reasons


def test_a_record_without_a_count_is_not_judged() -> None:
    found = record()
    del found["problems"]
    assert judged(found)[0] == "INCONCLUSIVE"


def test_refusals_do_not_pass_a_run_whose_test_did_not_fail_as_listed() -> None:
    wrong = [f"WRONG: {IN_THE_CALL.node}: error, expected failed on 'assert not failures'"]
    assert judged(record(), tests=wrong)[0] == "FINDING"
    assert judged(record(), tests=["WRONG: pytest wrote no results"])[0] == "FINDING"


def test_a_run_without_its_one_record_is_inconclusive() -> None:
    got, reasons = judged(record(), trouble=["the run left 0 new directories in .kills, not one"])
    assert (got, reasons) == (
        "INCONCLUSIVE",
        ["no record to judge: the run left 0 new directories in .kills, not one"],
    )


def test_the_api_s_words_are_printed_with_every_verdict() -> None:
    assert f"a failed job's error: {REFUSAL}" in judged(record())[1]
    assert f"a failed job's error: {REFUSAL}" in judged(record(failed_runs=5))[1]


def results(directory: Path, name: str, *lines: Any) -> None:
    (directory / name).mkdir(parents=True)
    text = "".join(line if isinstance(line, str) else json.dumps(line) + "\n" for line in lines)
    (directory / name / "results.jsonl").write_text(text)


OTHER = {"test": "failpoint after_tool_intent", "verdict": "passed"}


def test_only_the_record_the_run_left_is_read(tmp_path: Path) -> None:
    """`.kills` keeps earlier runs. A record of one of them is not this run's."""
    results(tmp_path, "2026-11-19T215549Z", record(model="an earlier run"))
    before = {"2026-11-19T215549Z"}
    read = switched_off.new_records
    assert read(tmp_path, before, "failpoint after_tool_ran") == (
        [],
        [f"the run left 0 new directories in {tmp_path.name}, not one"],
    )
    results(tmp_path, "2026-11-21T120000Z", OTHER, record())
    assert read(tmp_path, before, "failpoint after_tool_ran") == ([record()], [])
    results(tmp_path, "2026-11-21T120001Z", record())
    assert read(tmp_path, before, "failpoint after_tool_ran") == (
        [],
        [f"the run left 2 new directories in {tmp_path.name}, not one"],
    )
    assert read(tmp_path / "missing", set(), "failpoint after_tool_ran")[0] == []


@pytest.mark.parametrize(
    ("lines", "problem"),
    [
        ((), "has 0 records of 'failpoint after_tool_ran', not one"),
        ((OTHER,), "has 0 records of 'failpoint after_tool_ran', not one"),
        ((record(), record()), "has 2 records of 'failpoint after_tool_ran', not one"),
        ((record(), "not json\n"), "is not JSON"),
    ],
)
def test_a_run_that_did_not_leave_exactly_one_record_has_none(
    tmp_path: Path, lines: tuple[Any, ...], problem: str
) -> None:
    results(tmp_path, "2026-11-21T120000Z", *lines)
    found, trouble = switched_off.new_records(tmp_path, set(), "failpoint after_tool_ran")
    assert found == []
    assert len(trouble) == 1
    assert problem in trouble[0]


def test_a_directory_without_results_has_no_record(tmp_path: Path) -> None:
    (tmp_path / "2026-11-21T120000Z").mkdir()
    assert switched_off.new_records(tmp_path, set(), "failpoint after_tool_ran") == (
        [],
        ["2026-11-21T120000Z has no results.jsonl"],
    )


def test_a_test_that_failed_and_whose_teardown_failed_is_an_error(tmp_path: Path) -> None:
    """How pytest writes a void run: two cases for one test. The failure alone would pass."""
    name = "test_jobs_survive_their_worker_stopping_inside_the_call[after_tool_ran]"
    case = f'<testcase classname="tests.kills.test_kill_runs" name="{name}">'
    path = tmp_path / "junit.xml"
    path.write_text(
        '<testsuites><testsuite name="pytest">'
        f'{case}<failure message="AssertionError">&gt;   assert not failures</failure></testcase>'
        f'{case}<error message="failed on teardown">void run</error></testcase>'
        "</testsuite></testsuites>"
    )
    lines = switched_off.judge(KEYED, switched_off.outcomes(path))
    assert lines == [f"WRONG: {IN_THE_CALL.node}: error, expected failed on 'assert not failures'"]
    # The other order too.
    cases = path.read_text().split(case)
    path.write_text(case.join([cases[0], cases[2].replace("</testsuite></testsuites>", ""),
                               cases[1] + "</testsuite></testsuites>"]))  # fmt: skip
    assert switched_off.judge(KEYED, switched_off.outcomes(path)) == lines


# The rehearsal


def rehearsed_record(**changes: Any) -> dict[str, Any]:
    fake = {"provider": "fake provider", "model": "ci-triage-slow", "verdict": "passed"}
    none = {SENT: 0, ANSWERED: 0, REFUSED: 0, "failed_runs": 0, "problems": 0}
    return record(**{**fake, **none, "errors_of_failed_jobs": [], **changes})


def rehearsed(
    found: dict[str, Any], result: str = "passed", trouble: list[str] | None = None
) -> tuple[str, list[str]]:
    verdict, reasons = switched_off.judge_rehearsal(
        KEYED,
        {IN_THE_CALL.node: (result, "")} if result != "not run" else {},
        [] if trouble else [found],
        trouble or [],
        head=HEAD,
        rules=switched_off.kill_rules(),
    )
    return verdict, reasons


def test_a_rehearsal_is_right_only_when_nothing_was_sent_and_nothing_failed() -> None:
    assert rehearsed(rehearsed_record()) == (
        "REHEARSAL OK",
        [
            "5 jobs on the fake provider: none sent a rebuilt thinking block, all passed."
            " Not a result."
        ],
    )


@pytest.mark.parametrize(
    ("found", "result", "reason"),
    [
        (rehearsed_record(), "failed", "failed, expected passed"),
        (rehearsed_record(), "error", "error, expected passed"),
        (rehearsed_record(), "not run", "not run, expected passed"),
        (rehearsed_record(verdict="failed"), "passed", "verdict is 'failed', not 'passed'"),
        (
            rehearsed_record(verdict="void", void="the wall clock moved +9.0 s"),
            "passed",
            "verdict is 'void' (the wall clock moved +9.0 s)",
        ),
        (rehearsed_record(failed_runs=1), "passed", "failed_runs is 1, not 0"),
        (rehearsed_record(problems=1), "passed", "problems is 1, not 0"),
        (rehearsed_record(**{SENT: 1}), "passed", f"{SENT} is 1, not 0"),
        (rehearsed_record(**{REFUSED: None}), "passed", f"{REFUSED} is None, not 0"),
        (rehearsed_record(provider="Claude API"), "passed", "the record's provider"),
        (rehearsed_record(model="claude-fable-5-2"), "passed", "the record's model"),
        (rehearsed_record(runs=4), "passed", "the record's runs is 4, not 5"),
        (rehearsed_record(commit="fedcba9"), "passed", "the record's commit"),
    ],
)
def test_a_rehearsal_that_went_any_other_way_is_wrong(
    found: dict[str, Any], result: str, reason: str
) -> None:
    verdict, reasons = rehearsed(found, result)
    assert verdict == "REHEARSAL WRONG"
    assert any(reason in line for line in reasons), reasons


def test_a_rehearsal_without_its_one_record_is_wrong() -> None:
    assert rehearsed(rehearsed_record(), trouble=["no record"]) == (
        "REHEARSAL WRONG",
        ["no record"],
    )


# The workflow: which job a run reaches, and which step gets the key


def job(name: str) -> str:
    """The text of one job of the workflow."""
    lines = WORKFLOW.splitlines()
    start = lines.index(f"  {name}:")
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i][:2] == "  " and lines[i][2:3].strip()),
        len(lines),
    )
    return "\n".join(lines[start:end])


def steps(name: str) -> list[str]:
    """The steps of a job, each with the comment lines that follow it."""
    return ("\n" + job(name).split("    steps:\n", 1)[1]).split("\n      - ")[1:]


def index(found: list[str], text: str) -> int:
    (at,) = [i for i, step in enumerate(found) if text in step]
    return at


def test_the_job_on_the_fake_provider_holds_no_secret() -> None:
    fake = job("fake")
    assert "secrets." not in fake
    assert "environment:" not in fake
    assert "SWITCHED_OFF_PROVIDER" not in fake
    assert WORKFLOW.count("secrets.") == job("claude").count("secrets.") == 3
    assert WORKFLOW.count("environment: claude-api") == 1


def test_a_rehearsal_never_reaches_the_job_with_the_key() -> None:
    assert "    if: inputs.provider == 'fake' || inputs.suite == 'switched-off-rehearsal'" in job(
        "fake"
    )
    assert "    if: inputs.provider == 'claude' && inputs.suite != 'switched-off-rehearsal'" in job(
        "claude"
    )
    assert "make switched-off-rehearsal" in job("fake")
    assert "rehears" not in job("claude").split("    steps:")[1]


def test_the_key_reaches_a_step_only_after_the_refusals() -> None:
    claude = steps("claude")
    # No secret outside a step: not in the job's own `env`.
    assert "secrets." not in job("claude").split("    steps:")[0]
    with_key = [i for i, step in enumerate(claude) if "secrets." in step]
    off_main = index(claude, "name: Refuse a switched-off run off main")
    entry = index(claude, 'switched_off.py dispatch "$SWITCHED_OFF" claude "$MODEL"')
    assert off_main == 0
    assert "if: inputs.suite == 'switched-off' && github.ref != 'refs/heads/main'" in claude[0]
    assert "if: inputs.suite == 'switched-off'\n" in claude[entry]
    assert off_main < entry < min(with_key)
    run = index(claude, "SWITCHED_OFF_PROVIDER: claude")
    assert run in with_key
    assert "if: inputs.suite == 'switched-off'\n" in claude[run]
    assert "run: make switched-off\n" in claude[run]


def test_the_job_on_the_fake_provider_checks_the_entry_before_it_runs_it() -> None:
    fake = steps("fake")
    entry = index(fake, 'switched_off.py dispatch "$SWITCHED_OFF" fake')
    assert entry < index(fake, "run: make switched-off\n")
    assert "if: inputs.suite == 'switched-off' && github.ref != 'refs/heads/main'" in "".join(fake)


def test_the_reviewer_sees_which_entry_gets_the_key() -> None:
    name = job("claude").split("    if:")[0]
    assert "format('switched-off {0}', inputs.switched_off)" in name
