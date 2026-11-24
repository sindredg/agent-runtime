"""Stored text that a CI log prints never reads as a workflow command (issue 211).

Two layers. Each printer escapes the workflow-command markers: `json_safe`
for a JSON line, `text_safe` for plain text. Each workflow step that prints
stored text stops workflow commands while it prints, with a token made in
the step.
"""

import importlib
import importlib.util
import io
import json
import logging
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from runtime.logs import JsonFormatter, json_safe, text_safe

ROOT = Path(__file__).resolve().parents[2]

HOSTILE = (
    "first line\n::error::one\r  ::warning::two\r\n\t::notice::three"
    " and ##[error]four\u2028::group::five"
)
# The markers whole and in parts, line breaks, and whitespace the runner trims.
PARTS = ["::", "##[", "#", "[", ":", "a", " ", "\t", "\n", "\r", "\v", "\x1c", "\x85", "\xa0"]
MARKED = st.one_of(
    st.lists(st.sampled_from([*PARTS, "\u2028", "\\"]), max_size=12).map("".join),
    st.text(),
)


def commands(output: str) -> list[str]:
    """The lines of `output` that hold a workflow-command marker.

    Lines end at CR, LF or CR LF. `##[` counts anywhere in a line, `::`
    after the leading whitespace. Python's whitespace includes every
    character .NET trims.
    """
    return [
        line
        for line in re.split(r"\r\n|\r|\n", output)
        if "##[" in line or line.lstrip().startswith("::")
    ]


def test_the_check_finds_every_marker_of_the_hostile_text() -> None:
    assert commands(HOSTILE) == [
        "::error::one",
        "  ::warning::two",
        "\t::notice::three and ##[error]four\u2028::group::five",
    ]


# The helpers


@example({HOSTILE: HOSTILE})
@given(st.dictionaries(MARKED, MARKED, max_size=4))
def test_a_json_line_keeps_its_value_and_holds_no_marker(value: dict[str, str]) -> None:
    line = json_safe(json.dumps(value))
    assert commands(line) == []
    assert json.loads(line) == value


@example(HOSTILE)
@given(MARKED)
def test_plain_text_holds_no_marker_and_keeps_its_line_breaks(text: str) -> None:
    escaped = text_safe(text)
    assert commands(escaped) == []
    assert re.findall(r"\r\n|\r|\n", escaped) == re.findall(r"\r\n|\r|\n", text)


def test_plain_text_without_a_marker_is_left_as_it_is() -> None:
    text = "ok: tests/kills/test_kill_runs.py::test_x: failed, expected failed on 'a :: b'"
    assert text_safe(text) == text
    assert text_safe("  ::x") == "  \\u003a:x"
    assert text_safe("a ##[b") == "a #\\u0023[b"


# The worker's JSON log


def test_a_log_line_holds_no_marker_and_decodes_to_the_same_text() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    log = logging.getLogger("test.workflow_commands")
    log.handlers[:] = [handler]
    log.propagate = False
    log.setLevel(logging.INFO)
    try:
        raise ValueError(HOSTILE)
    except ValueError:
        log.exception(HOSTILE, extra={"error": HOSTILE})

    output = stream.getvalue()
    assert commands(output) == []
    (line,) = output.splitlines()
    entry = json.loads(line)
    assert entry["message"] == entry["error"] == HOSTILE
    assert HOSTILE in entry["exception"]


# scripts/switched_off.py


def load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before it runs: the dataclasses in it look their module up.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


switched_off = load("switched_off_printer", ROOT / "scripts" / "switched_off.py")
KEYED = "thinking-signature-changed"
HEAD = "0123456789abcdef0123456789abcdef01234567"


@pytest.mark.parametrize(
    ("rehearsal", "changes"),
    [
        # A run on the Claude API prints each failed job's error.
        (False, {"errors_of_failed_jobs": [HOSTILE]}),
        # A rehearsal prints why the record is void.
        (True, {"verdict": "void", "void": HOSTILE}),
    ],
)
def test_a_switched_off_run_prints_no_marker_of_its_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    rehearsal: bool,
    changes: dict[str, Any],
) -> None:
    entry = switched_off.ENTRIES[KEYED]
    for edit in entry.edits:
        (tmp_path / edit.path).parent.mkdir(parents=True, exist_ok=True)
    record = {
        "test": entry.record.test,
        "commit": HEAD[:7],
        "provider": "fake provider" if rehearsal else "Claude API",
        "model": "ci-triage-slow" if rehearsal else entry.model,
        "runs": entry.runs,
        **dict.fromkeys(switched_off.JUDGED, 0),
        "verdict": "failed",
        **changes,
    }

    def pytest_run(args: list[str], **more: Any) -> Any:
        # The run's own record, left where the kill runs leave theirs.
        directory = tmp_path / ".kills" / "2026-11-24T000000Z"
        directory.mkdir(parents=True)
        (directory / "results.jsonl").write_text(json.dumps(record) + "\n")
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(switched_off, "ROOT", tmp_path)
    monkeypatch.setattr(switched_off, "_git", lambda *args: "" if args[0] == "status" else HEAD)
    monkeypatch.setattr(switched_off, "_sh", lambda *args, env: None)
    monkeypatch.setattr(switched_off.subprocess, "run", pytest_run)
    monkeypatch.delenv("CLAUDE_API_KEY", raising=False)
    if rehearsal:
        monkeypatch.delenv("SWITCHED_OFF_PROVIDER", raising=False)
    else:
        monkeypatch.setenv("SWITCHED_OFF_PROVIDER", "claude")

    assert switched_off.run(KEYED, rehearsal=rehearsal) == 1
    out = capsys.readouterr().out
    assert commands(out) == []
    # The record's text is printed, escaped.
    assert "#\\u0023[error]four" in out


# The chaos and kill runs' results, and the lab demo


def test_a_chaos_result_line_holds_no_marker(
    chaos: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(chaos, "CHAOS_DIR", tmp_path)
    chaos._pending.append({"test": "a test", "error": HOSTILE})
    chaos._write_results("void", void=HOSTILE)
    file = (tmp_path / "results.jsonl").read_text()
    printed = capsys.readouterr().out
    assert commands(file) == commands(printed) == []
    assert json.loads(file)["error"] == json.loads(file)["void"] == HOSTILE
    assert printed.strip() == f"RESULT {file.strip()}"


@pytest.fixture
def kills(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """The kill runs' conftest, imported as pytest imports it. Nothing in it starts the stack."""
    monkeypatch.syspath_prepend(str(ROOT / "tests"))
    return importlib.import_module("kills.conftest")


def test_a_kill_run_result_line_holds_no_marker(
    kills: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    kills._pending.append({"test": "a test", "errors_of_failed_jobs": [HOSTILE]})
    kills._write_results(tmp_path, "void", void=HOSTILE)
    file = (tmp_path / "results.jsonl").read_text()
    printed = capsys.readouterr().out
    assert commands(file) == commands(printed) == []
    assert json.loads(file)["errors_of_failed_jobs"] == [HOSTILE]
    assert printed.strip() == f"RESULT {file.strip()}"


def test_a_kill_runs_problem_lines_hold_no_marker(
    kills: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    rules = importlib.import_module("kills.rules")
    job = {
        "id": "a job",
        "status": "failed",
        "error": HOSTILE,
        "takeovers": 0,
        "lease_epoch": 1,
        "tokens_used": 10,
        "token_budget": rules.TOKEN_BUDGET,
    }
    run = rules.Run(job, [], {}, note=HOSTILE)
    found = rules.problems(run)
    assert commands("\n".join(found))
    kills.show(f"  0 FAIL {run.summary()}")
    kills.show("\n".join(found))
    out = capsys.readouterr().out
    assert commands(out) == []
    assert "#\\u0023[error]four" in out


def test_the_lab_demo_prints_no_marker(capsys: pytest.CaptureFixture[str]) -> None:
    lab = load("lab_conftest_printer", ROOT / "tests" / "lab" / "conftest.py")
    lab.say(HOSTILE)
    lab.show(f"\nLab worker logs:\n{HOSTILE}")
    out = capsys.readouterr().out
    assert commands(out) == []
    assert out.count("#\\u0023[error]four") == 2


# The workflows

WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
# The make targets that run tests, start the stack or print its logs. A
# test below keeps the list equal to the Makefile's.
TARGETS = (
    "all",
    "up",
    "up-claude",
    "logs",
    "test",
    "integration",
    "escape",
    "chaos",
    "notify-timing",
    "switched-off",
    "switched-off-rehearsal",
    "lab",
    "kills",
    "kills-claude",
)
# What runs tests or the stack, or prints stored text: pytest, one of the
# targets, a service's logs, or a record file.
PRINTS = re.compile(
    r"\bpytest\b"
    rf"|\bmake ({'|'.join(map(re.escape, TARGETS))})(?![\w-])"
    r"|docker compose .*\blogs\b"
    r"|\.(chaos|kills|notify-timing|contract)/"
)
STOP = ["token=$(openssl rand -hex 16)", 'echo "::stop-commands::$token"']
RESUME = 'echo "::$token::"'


def runs(workflow: Path) -> list[tuple[str, str, list[str]]]:
    """(job, step name, lines of its `run`) for every step with a `run`."""
    found: list[tuple[str, str, list[str]]] = []
    job = name = ""
    script: list[str] | None = None
    for line in [*workflow.read_text().splitlines(), "end:"]:
        indent = len(line) - len(line.lstrip(" "))
        if script is not None and (indent >= 10 or not line.strip()):
            script.append(line[10:])
            continue
        if script is not None:
            found.append((job, name, [s for s in script if s.strip()]))
            script = None
        if indent == 2 and line.rstrip().endswith(":") and not line.lstrip().startswith("#"):
            job = line.strip().removesuffix(":")
        elif indent == 6 and line.lstrip().startswith("- "):
            name = line.split("- name: ", 1)[1] if "- name: " in line else ""
        if line.startswith(("        run: ", "      - run: ")):
            command = line.split("run: ", 1)[1]
            script = [] if command in ("|", "|-", ">", ">-") else [command]
    return found


def printing_steps() -> dict[tuple[str, str, str], list[str]]:
    return {
        (workflow.name, job, name): script
        for workflow in WORKFLOWS
        for job, name, script in runs(workflow)
        if any(PRINTS.search(line) for line in script)
    }


def test_every_step_that_runs_tests_or_prints_stored_text_stops_workflow_commands() -> None:
    steps = printing_steps()
    unwrapped = [
        step
        for step, script in steps.items()
        if script[:2] != STOP
        or script[-1] != RESUME
        or sum("token" in line for line in script) != 3
    ]
    assert unwrapped == []
    assert set(steps) == {
        ("ci.yml", "python", "Unit tests"),
        ("ci.yml", "stack", "Start the stack"),
        ("ci.yml", "stack", "Service logs"),
        ("ci.yml", "integration", "Start the stack"),
        ("ci.yml", "integration", "Integration tests"),
        ("ci.yml", "integration", "Service logs"),
        ("ci.yml", "escape", "Escape tests"),
        ("ci.yml", "chaos", "Chaos tests"),
        ("ci.yml", "chaos", "Results"),
        ("ci.yml", "chaos", "Service logs"),
        ("ci.yml", "lab", "Lab demo"),
        ("ci.yml", "lab", "Service logs"),
        ("ci.yml", "kills", "Kill runs"),
        ("ci.yml", "kills", "Results"),
        ("ci.yml", "kills", "Service logs"),
        ("manual-runs.yml", "fake", "Kill runs"),
        ("manual-runs.yml", "fake", "Switched-off run"),
        ("manual-runs.yml", "fake", "Timed notification run"),
        ("manual-runs.yml", "fake", "Rehearsal of a switched-off run"),
        ("manual-runs.yml", "fake", "Results"),
        ("manual-runs.yml", "fake", "Service logs"),
        ("manual-runs.yml", "fake-entries", "Switched-off run"),
        ("manual-runs.yml", "fake-entries", "Results"),
        ("manual-runs.yml", "fake-entries", "Service logs"),
        ("manual-runs.yml", "claude", "Kill runs on the Claude API"),
        ("manual-runs.yml", "claude", "Switched-off run on the Claude API"),
        ("manual-runs.yml", "claude", "Contract test"),
        ("manual-runs.yml", "claude", "Results"),
        ("manual-runs.yml", "claude", "Service logs"),
    }


def test_the_targets_are_every_make_target_that_runs_tests_or_the_stack() -> None:
    """A target that runs pytest, starts the stack, prints its logs, or needs one that does."""
    found: dict[str, tuple[list[str], list[str]]] = {}
    target = ""
    for line in (ROOT / "Makefile").read_text().splitlines():
        if match := re.match(r"([a-z][\w-]*):(?!=)(.*)", line):
            target = match[1]
            found[target] = (match[2].split(), [])
        elif line.startswith("\t") and target:
            found[target][1].append(line)
    runs_tests = {
        name
        for name, (_, recipe) in found.items()
        if any(re.search(r"\bpytest\b|compose .*\b(up|logs)\b|switched_off\.py", r) for r in recipe)
    }
    runs_tests |= {name for name, (needs, _) in found.items() if set(needs) & runs_tests}
    assert runs_tests == set(TARGETS)


def test_the_wrapper_stops_and_resumes_with_one_new_token_each_time() -> None:
    def tokens() -> tuple[str, str]:
        done = subprocess.run(  # noqa: S603 - the workflow's own lines
            ["bash", "-e", "-c", "\n".join([*STOP, "echo stored text", RESUME])],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        stop, printed, resume = done.stdout.splitlines()
        assert printed == "stored text"
        token = stop.removeprefix("::stop-commands::")
        assert re.fullmatch(r"[0-9a-f]{32}", token)
        return token, resume

    first, resume = tokens()
    assert resume == f"::{first}::"
    assert tokens()[0] != first
