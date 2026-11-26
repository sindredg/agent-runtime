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
from dataclasses import dataclass, field
from datetime import UTC, datetime
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


def test_a_chaos_runs_problem_and_lease_lines_hold_no_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "tests"))
    conftest = importlib.import_module("chaos.conftest")
    random_kills = importlib.import_module("chaos.test_kills")
    run = conftest.Run({"status": "failed", "error": HOSTILE}, [], [], {}, note=HOSTILE)
    problems = random_kills.verdict(run, [])
    entry = {"job_id": "a job", "message": "lease lost", "time": HOSTILE, "exception": HOSTILE}
    lease = conftest.lease_trouble(json.dumps(entry), "a job")
    assert len(problems) == len(lease) == 1
    assert commands("\n".join(problems + lease)) == []
    assert "#\\u0023[error]four" in problems[0]


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


def test_a_kill_runs_failure_message_and_list_hold_no_marker(kills: ModuleType) -> None:
    """pytest prints the message of `assert not failures` and the list itself."""
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
    failures = [kills.failure("run 0", p, run) for p in rules.problems(run)]
    assert failures
    assert commands("\n".join(failures)) == commands(repr(failures)) == []


@pytest.fixture
def long_job(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """The long-job suite's conftest, imported as pytest imports it. It starts no stack."""
    monkeypatch.syspath_prepend(str(ROOT / "tests"))
    return importlib.import_module("long.conftest")


def test_a_long_job_result_line_and_failure_line_hold_no_marker(
    long_job: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The reason of a question is stored text, and it is in a failure line."""
    stack = long_job.Stack(tmp_path)
    run = long_job.Running("a run", datetime.now(UTC), 0.0, 0.0)
    run.counts = {"jobs": 1, "status": HOSTILE}
    stack.write(run, "void", HOSTILE)
    stack.hang_up()
    file = (tmp_path / "results.jsonl").read_text()
    printed = capsys.readouterr().out
    assert commands(file) == commands(printed) == []
    assert (json.loads(file)["status"], json.loads(file)["void"]) == (HOSTILE, HOSTILE)
    assert printed.strip() == f"RESULT {file.strip()}"

    line = long_job.failure("the job asked a person a question", f"question 3: {HOSTILE}")
    # `show` gets stored text as it is, not a line that is escaped already.
    long_job.show(f"question 3: {HOSTILE}")
    out = capsys.readouterr().out
    assert "#\\u0023[error]four" in out
    # pytest prints the message of `assert not failures` and the list itself.
    assert commands(line) == commands(repr([line])) == commands(out) == []
    assert "#\\u0023[error]four" in line


def test_the_contract_report_holds_no_marker(tmp_path: Path) -> None:
    contract = load("contract_printer", ROOT / "tests" / "contract" / "test_messages_contract.py")
    usage = {"input_tokens": 1, "output_tokens": 1}
    fake = [
        {"content": [{"type": "text", "text": "ok"}], "usage": usage, "stop_reason": "end_turn"}
    ]
    real = [
        {
            "content": [{"type": "text", "text": HOSTILE}],
            "usage": usage,
            "stop_reason": HOSTILE,
        }
    ]
    contract.write_report(tmp_path / "report", fake, real)
    written = {path.name: path.read_text() for path in (tmp_path / "report").iterdir()}
    assert sorted(written) == ["api-only.txt", "api.json", "fake.json"]
    assert [name for name, text in written.items() if commands(text)] == []
    assert json.loads(written["api.json"]) == real
    assert json.loads(written["fake.json"]) == fake
    assert "#\\u0023[error]four" in written["api-only.txt"]


def test_the_lab_demo_prints_no_marker(capsys: pytest.CaptureFixture[str]) -> None:
    lab = load("lab_conftest_printer", ROOT / "tests" / "lab" / "conftest.py")
    lab.say(HOSTILE)
    lab.show(f"\nLab worker logs:\n{HOSTILE}")
    out = capsys.readouterr().out
    assert commands(out) == []
    assert out.count("#\\u0023[error]four") == 2


# The workflows

WORKFLOWS = sorted(
    path
    for pattern in ("*.yml", "*.yaml")
    for path in (ROOT / ".github" / "workflows").glob(pattern)
)
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
    "long-up",
    "long",
    "long-job",
)
# What runs tests or the stack, or prints stored text: pytest, one of the
# targets, a container's logs, or a record file.
PRINTS = re.compile(
    r"\bpytest\b"
    rf"|\bmake ({'|'.join(map(re.escape, TARGETS))})(?![\w-])"
    r"|\bdocker\b.*\blogs\b"
    r"|\.(chaos|kills|long|long-job|notify-timing|contract)/"
)
# The steps with a `run` that print nothing from the stack or the tests:
# lint of the repository's own files, setup, checks of inputs, and stops.
QUIET = {
    "Sync the environment",
    "ruff check",
    "ruff format",
    "mypy",
    "yamllint",
    "JSON syntax",
    "SVG syntax",
    "Docs limits",
    "actionlint",
    "hadolint",
    "checkmake",
    "Docker socket group",
    "Validate the compose files",
    "Call the API from the host",
    "Gitea is stopped after the demo",
    "Stop the stack",
    "Refuse the contract test",
    "Refuse a switched-off run off main",
    "Check the model name",
    "Check the entry against the provider",
    "Check the entry against the provider and the model",
    "Names",
    "Commit",
}
STOP = ["exec 2>&1", "token=$(openssl rand -hex 16)", 'echo "::stop-commands::$token"']
RESUME = 'echo "::$token::"'


@dataclass
class Step:
    job: str
    name: str = ""
    uses: str = ""
    run: list[str] | None = None
    given: dict[str, str] = field(default_factory=dict)


def indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def scalar(text: str) -> str:
    """A plain value without a trailing comment or its quotes."""
    text = re.sub(r"\s+#.*$", "", text).strip()
    quoted = len(text) > 1 and text[0] == text[-1] and text[0] in "'\""
    return text[1:-1] if quoted else text


def steps_of(text: str) -> list[Step]:
    """Every step of every job, read line by line at whatever indentation the file uses."""
    lines = text.splitlines()
    found: list[Step] = []

    def nested(start: int, above: int) -> tuple[list[str], int]:
        """The lines from `start` that are blank or deeper than `above`, and where they end."""
        end = start
        while end < len(lines) and (not lines[end].strip() or indent(lines[end]) > above):
            end += 1
        return lines[start:end], end

    def key(step: Step, text: str, at: int, start: int) -> int:
        name, _, rest = text.partition(":")
        rest = rest.strip()
        inner, end = nested(start, at)
        if name == "name":
            step.name = scalar(rest)
        elif name == "uses":
            step.uses = scalar(rest)
        elif name == "run" and rest[:1] in ("|", ">") and rest:
            body = [line for line in inner if line.strip()]
            cut = min((indent(line) for line in body), default=0)
            step.run = [line[cut:] for line in body]
        elif name == "run":
            # A plain or quoted value may go on over the next, deeper lines.
            step.run = [rest, *(line.strip() for line in inner if line.strip())]
        elif name == "with":
            for line in inner:
                if line.strip() and not line.strip().startswith("#"):
                    given, _, value = line.strip().partition(":")
                    step.given[given.strip()] = scalar(value)
        return end

    job, job_at, steps_at, dash_at = "", -1, -1, -1
    in_jobs, i = False, 0
    while i < len(lines):
        line = lines[i]
        text, at = line.strip(), indent(line)
        if not text or text.startswith("#"):
            i += 1
        elif at == 0:
            in_jobs, job_at, steps_at, dash_at = scalar(text) == "jobs:", -1, -1, -1
            i += 1
        elif not in_jobs:
            i += 1
        elif at <= job_at or job_at < 0:
            job_at, job, steps_at, dash_at = at, text.split(":", 1)[0], -1, -1
            i += 1
        elif text.startswith("- ") and 0 <= steps_at <= at and dash_at in (-1, at):
            dash_at, step = at, Step(job)
            found.append(step)
            first = text[2:].lstrip(" ")
            key_at = at + len(text) - len(first)
            i = key(step, first, key_at, i + 1)
            while i < len(lines):
                if not lines[i].strip() or lines[i].strip().startswith("#"):
                    i += 1
                elif lines[i].strip() and indent(lines[i]) == key_at:
                    i = key(step, lines[i].strip(), key_at, i + 1)
                else:
                    break
        elif text.startswith("steps:"):
            steps_at, dash_at = at, -1
            i += 1
        else:
            if at <= steps_at:
                steps_at, dash_at = -1, -1
            i = nested(i + 1, at)[1]
    return found


# A `run` or `uses` key, as a plain search sees it, whatever the reader makes of the file.
KEYS = re.compile(r"""^\s*(?:-\s+)?["']?(run|uses)["']?\s*:""", re.MULTILINE)


def every_step() -> list[tuple[str, Step]]:
    found = []
    for path in WORKFLOWS:
        text = path.read_text()
        steps = steps_of(text)
        # The reader missed no step: it found as many as a plain search does.
        assert steps, f"{path.name}: no step was read"
        # A step in flow style, `- {run: ...}`, is not read: write it as a block.
        assert not re.search(r"^\s*-\s*\{", text, re.MULTILINE), f"{path.name}: a flow-style item"
        counted = [found.group(1) for found in KEYS.finditer(text)]
        assert sum(step.run is not None for step in steps) == counted.count("run"), path.name
        assert sum(bool(step.uses) for step in steps) == counted.count("uses"), path.name
        found += [(path.name, step) for step in steps]
    assert len(found) > 100, "the workflows were not read"
    return found


def unwrapped(steps: list[tuple[str, Step]]) -> list[tuple[str, str, str]]:
    """The steps with a `run` that are neither quiet nor wrapped, and quiet ones that print."""
    wrong = []
    for file, step in steps:
        if step.run is None:
            continue
        if step.name in QUIET:
            ok = not any(PRINTS.search(line) for line in step.run)
        else:
            ok = (
                step.run[:3] == STOP
                and step.run[-1] == RESUME
                and sum("token" in line for line in step.run) == 3
            )
        if not ok:
            wrong.append((file, step.job, step.name))
    return wrong


def test_every_step_with_a_run_stops_workflow_commands_unless_it_prints_nothing_stored() -> None:
    steps = every_step()
    assert unwrapped(steps) == []
    # Every quiet name is a step's: the list holds nothing stale.
    named = {step.name for _, step in steps if step.run is not None}
    assert QUIET - named == set()
    wrapped = {
        (file, step.job, step.name) for file, step in steps if step.run and step.run[:3] == STOP
    }
    assert ("ci.yml", "integration", "Integration tests") in wrapped
    assert ("manual-runs.yml", "claude", "Contract test") in wrapped
    assert len(wrapped) == 35


def test_the_reader_takes_any_indentation() -> None:
    text = (
        "on: push\n"
        "jobs:  # every job\n"
        "    build:\n"
        "        runs-on: x\n"
        "        steps:\n"
        "        -   uses: astral-sh/setup-uv@abc  # v1\n"
        "            with:\n"
        "                version: '1'\n"
        "        # a comment between steps\n"
        "        -   name: Worker logs\n"
        "            run: docker logs worker-1\n"
        "        -   name: Quiet\n"
        "            run: |\n"
        "              exec 2>&1\n"
        "\n"
        "              echo done\n"
        "\n"
        "            if: always()\n"
        "        env:\n"
        "            A: b\n"
        "    other:\n"
        "      steps:\n"
        "        - run: make up\n"
        "        - name: Stop the stack\n"
        "          run: make clean &&\n"
        "            make chaos\n"
    )
    steps = steps_of(text)
    assert [(s.job, s.name, s.uses, s.run, s.given) for s in steps] == [
        ("build", "", "astral-sh/setup-uv@abc", None, {"version": "1"}),
        ("build", "Worker logs", "", ["docker logs worker-1"], {}),
        ("build", "Quiet", "", ["exec 2>&1", "echo done"], {}),
        ("other", "", "", ["make up"], {}),
        ("other", "Stop the stack", "", ["make clean &&", "make chaos"], {}),
    ]
    assert unwrapped([("x.yaml", step) for step in steps]) == [
        ("x.yaml", "build", "Worker logs"),
        ("x.yaml", "build", "Quiet"),
        ("x.yaml", "other", ""),
        ("x.yaml", "other", "Stop the stack"),
    ]


def test_the_workflows_use_only_actions_whose_problem_matchers_are_known() -> None:
    """checkout removes its own matcher in its step; setup-uv's is turned off below.

    A new action may add matchers that stop-commands does not stop: read its
    action before adding it here.
    """
    used = {step.uses.split("@")[0] for _, step in every_step() if step.uses}
    assert used == {"actions/checkout", "astral-sh/setup-uv"}


def test_setup_uv_runs_without_its_problem_matchers() -> None:
    setups = [
        (file, step.job, step.given.get("add-problem-matchers"))
        for file, step in every_step()
        if step.uses.startswith("astral-sh/setup-uv@")
    ]
    assert len(setups) == 13
    assert [setup for setup in setups if setup[2] != "false"] == []


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
