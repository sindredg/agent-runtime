"""How a long job on a real model is judged, on made-up logs with no stack and no key.

The made-up job reads ten files in seven windows and opens an issue for
each violation it finds. Six summaries lie between the windows. It passes
with no clause. Each test changes one thing in it, by a named helper, and
names every clause the grader then reports.

The grants: `M[0]` to `M[6]` are made violations by a notice, and `M[0]` is
read before the first summary and has no issue. `C1` and `C2` are cleared
by a notice. `K1` and `K2` are decided by a fact in an earlier file. `X0`
is decided by its own file and filed before the first summary.

The last tests run the worker loop in process against the fake provider and
the fake tracker, with the generated job of the committed seed.
"""

import asyncio
import copy
import dataclasses
import functools
import hashlib
import importlib.util
import json
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient

from access_review import SHORT, answer_key, read_seed
from fake_provider.app import create_app
from fake_provider.messages import encode_sse, stream_events
from fake_provider.scenario import parse_scenario
from fake_tracker.app import create_app as create_tracker
from runtime.provider.messages import AnthropicProvider
from runtime.spec import JobSpec
from runtime.store.jobs import DEADLINE_ERROR, TAKEOVER_LIMIT_ERROR, Job
from runtime.store.status import JobStatus
from runtime.store.steps import Step, StepKind
from runtime.tools.base import ToolRegistry
from runtime.tools.create_issue import create_issue_tool
from runtime.tools.read_file import READ_FILE
from runtime.tools.trackers import FakeTracker
from runtime.worker import agent
from runtime.worker.halt import Halt
from runtime.worker.lease import Lease

ROOT = Path(__file__).resolve().parents[2]
SUITE = ROOT / "tests" / "long_job"
JOB = uuid.UUID("01a1d655-88bd-7468-afc4-5ca3a194790b")
START = datetime(2026, 11, 26, 1, 0, 0, tzinfo=UTC)
NOW = datetime.now(UTC)
LOCAL = "http://localhost"
CODE = "7Q2K-M4TZ-9XBD-HW3N"
M = [f"GR0000000M{n}" for n in range(7)]
C1, C2 = "GR0000000C1", "GR0000000C2"
K1, K2 = "GR0000000K1", "GR0000000K2"
X0 = "GR0000000X0"
# A grant of the organisation that this job never touches.
UNSEEN = "GR0000000Z9"
INDEX = 0
TOOLS = ["read_file", "create_issue"]
# The file each grant is in, by its number.
WHERE = {M[0]: 1, X0: 1, M[1]: 2, C1: 3, M[2]: 4, C2: 5, M[3]: 6, M[4]: 7, K1: 8, M[5]: 9, K2: 9}
WHERE[M[6]] = 10
# The file that holds the fact each carried grant is decided by.
FACT = {K1: 2, K2: 7}


@functools.cache
def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"long_job_{name}_graded", SUITE / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before it runs: the dataclasses in it look their module up.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def grader() -> ModuleType:
    return load("grader")


@pytest.fixture(scope="module")
def rules() -> ModuleType:
    return load("rules")


@pytest.fixture(scope="module")
def counting() -> ModuleType:
    return load("counting")


# The made-up job


def path(number: int) -> str:
    return "index.txt" if number == INDEX else f"access/{number:04d}.txt"


def text(number: int) -> str:
    """The content of a file: its own line and a fill, different for each file."""
    return f"access records, file {number:04d}\n" + "=" * (40 + number) + "\n"


def digest(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def title(grant: str) -> str:
    return f"{grant}: holder has left {CODE}"


def read(number: int) -> tuple[Any, ...]:
    return ("read", number, text(number), False)


def issue(grant: str, words: str | None = None) -> tuple[Any, ...]:
    return ("issue", grant, title(grant) if words is None else words)


type Plan = list[list[tuple[Any, ...]]]


def plan() -> Plan:
    """What the job does in each window. A summary follows every window but the last."""
    return [
        [read(INDEX), read(1), issue(X0, f"{X0}: missing from the later roster")],
        [read(2), issue(M[1]), read(3)],
        [read(4), issue(M[2]), read(5)],
        [read(6), issue(M[3])],
        [read(7), issue(M[4]), read(8), issue(K1)],
        [read(9), issue(M[5])],
        [read(10), issue(M[6])],
    ]


def without_issue(windows: Plan, grant: str) -> Plan:
    """`missed`: the job never opens the issue of `grant`."""
    found = [[a for a in window if a[:2] != ("issue", grant)] for window in windows]
    assert sum(map(len, found)) == sum(map(len, windows)) - 1
    return found


def retitled(windows: Plan, grant: str, words: str) -> Plan:
    """The one issue of `grant` gets another title."""
    assert sum(action[:2] == ("issue", grant) for window in windows for action in window) == 1
    return [
        [issue(grant, words) if a[:2] == ("issue", grant) else a for a in window]
        for window in windows
    ]


def with_action(windows: Plan, window: int, action: tuple[Any, ...], at: int | None = None) -> Plan:
    """One more action in a window: at its end, or before the action at `at`."""
    found = copy.deepcopy(windows)
    found[window].insert(len(found[window]) if at is None else at, action)
    return found


def without_read(windows: Plan, number: int) -> Plan:
    found = [[a for a in window if a[:2] != ("read", number)] for window in windows]
    assert sum(map(len, found)) == sum(map(len, windows)) - 1
    return found


def read_early(windows: Plan, number: int) -> Plan:
    """The file is read in the first window too, so its grants are not counted."""
    return with_action(windows, 0, read(number))


def issues_in_two_windows(windows: Plan) -> Plan:
    """Every issue after the first summary is opened in the second or the last window."""
    moved = [a for window in windows[2:-1] for a in window if a[0] == "issue"]
    kept = [[a for a in w if a[0] != "issue"] if 2 <= n < 6 else w for n, w in enumerate(windows)]
    kept[-1] = [*kept[-1], *moved]
    return kept


def one_summary_less(windows: Plan) -> Plan:
    """The last two windows are one: five summaries."""
    return [*windows[:-2], [*windows[-2], *windows[-1]]]


@dataclasses.dataclass
class Made:
    """A built job: its steps, the tracker's issues for it, and the harness kills."""

    steps: list[dict[str, Any]]
    issues: list[dict[str, Any]]
    kills: list[dict[str, Any]]

    def seqs(self, kind: str) -> list[int]:
        return [step["seq"] for step in self.steps if step["kind"] == kind]


class Builder:
    def __init__(self) -> None:
        self.steps: list[dict[str, Any]] = []
        self.issues: list[dict[str, Any]] = []
        self.calls = 0

    def add(self, kind: str, **payload: Any) -> dict[str, Any]:
        seq = len(self.steps) + 1
        at = (START + timedelta(seconds=seq)).isoformat()
        step = {"seq": seq, "kind": kind, "payload": payload, "lease_epoch": 1, "created_at": at}
        self.steps.append(step)
        return step

    def request(self, **extra: Any) -> None:
        self.add(
            "model_request",
            model="claude-haiku-5-5",
            max_tokens=32_000,
            messages=1,
            input_bound=2_000,
            tokens_used=0,
            summary_trigger=80_000,
            tools=list(TOOLS),
            **extra,
        )

    def answer(self, kind: str, content: list[dict[str, Any]], stop: str, **extra: Any) -> None:
        usage = {"input_tokens": 1_000, "output_tokens": 50}
        number = len(self.steps)
        self.add(kind, id=f"msg_{number}", content=content, stop_reason=stop, usage=usage, **extra)

    def call(self, name: str, given: dict[str, str], content: str, is_error: bool) -> str:
        self.calls += 1
        call_id = f"toolu_{self.calls:03d}"
        block = {"type": "tool_use", "id": call_id, "name": name, "input": given}
        self.request()
        self.answer("model_response", [block], "tool_use")
        self.add("tool_intent", tool_use_id=call_id, name=name, input=given)
        self.add("tool_result", tool_use_id=call_id, content=content, is_error=is_error)
        return call_id

    def act(self, action: tuple[Any, ...]) -> None:
        if action[0] == "read":
            _, number, content, is_error = action
            self.call("read_file", {"path": path(number)}, content, is_error)
        elif action[0] == "issue":
            _, _, words = action
            number = 100_000 + len(self.issues)
            created = f"created issue {number}: {words}"
            given = {"title": words, "body": "See the file."}
            call_id = self.call("create_issue", given, created, False)
            self.issues.append(
                {
                    "id": number,
                    "title": words,
                    "body": "See the file.",
                    "operation_id": f"{JOB}:{call_id}",
                    "created_at": START.isoformat(),
                }
            )
        else:
            _, name, given, content = action
            self.call(name, given, content, True)

    def summary(self, stop: str = "end_turn", **extra: Any) -> None:
        self.request(summary=True)
        words = [{"type": "text", "text": "The task and what was read."}]
        if "problem" not in extra:
            extra["message"] = {"role": "user", "content": words}
        self.answer("summary", words, stop, **extra)


def build(
    windows: Plan,
    *,
    ending: tuple[Any, ...] = ("response", "end_turn"),
    kills: Sequence[tuple[int, int]] = ((2, 2), (4, 2)),
) -> Made:
    """The job of `windows`, and its three kills.

    `ending` is the last answer: ("response", stop reason, extra payload) or
    ("summary", stop reason, extra payload). A harness kill is (the summary
    it follows, steps after it): the worker's last step is that one. The
    first worker's last step is the first summary.
    """
    builder = Builder()
    for number, window in enumerate(windows):
        for action in window:
            builder.act(action)
        if number < len(windows) - 1:
            builder.summary()
    kind, stop, *extra = ending
    if kind == "summary":
        builder.summary(stop, **(extra[0] if extra else {}))
    else:
        builder.request()
        done = [{"type": "text", "text": "Every file is read."}]
        builder.answer("model_response", done, stop, **(extra[0] if extra else {}))
    made = Made(builder.steps, builder.issues, [])
    summaries = made.seqs("summary")
    ends = [summaries[0], *(summaries[after - 1] + steps for after, steps in kills)]
    for step in made.steps:
        step["lease_epoch"] = 1 + sum(step["seq"] > end for end in ends)
    made.kills = [{"target": end, "epoch": epoch} for epoch, end in enumerate(ends[1:], start=2)]
    return made


def answers(grader: ModuleType) -> Any:
    files = {grant: digest(text(number)) for grant, number in WHERE.items() if grant != X0}
    return grader.Answers(
        made=tuple(M),
        cleared=(C1, C2),
        violations=frozenset({*M, K1, K2, X0}),
        grants=frozenset({*M, C1, C2, K1, K2, X0, UNSEEN}),
        code=CODE,
        files=files,
        carried=tuple((grant, digest(text(number))) for grant, number in FACT.items()),
        corpus={digest(text(number)): len(text(number).encode()) for number in range(1, 11)},
    )


def view(status: str = "succeeded", error: str = "none", takeovers: int = 3) -> dict[str, Any]:
    """A job as the harness reduces it: counts, a status and the class of its error."""
    # Only a class the reduce function can give: a made-up name would test nothing.
    assert error in load("rules").ERROR_CLASSES
    return {
        "status": status,
        "takeovers": takeovers,
        "lease_owner": None,
        "lease_expires_at": None,
        "tokens_used": 829_211,
        "deadline": "2026-11-26T06:10:00Z",
        "error": error,
        "question": None,
    }


def log(grader: ModuleType, made: Made, **changes: Any) -> Any:
    """What the harness hands the grader for `made`: nothing wrong unless `changes` says so."""
    kills = [grader.Kill(held=True, latency=0.4, problem=None, **kill) for kill in made.kills]
    given: dict[str, Any] = {
        "job": view(),
        "steps": made.steps,
        "issues": made.issues,
        "questions": [],
        "kills": kills,
        "failpoint_lines": 1,
        "verify": 0,
        "count": SimpleNamespace(total=1_000_000, chunks=4, partial=False),
        "count_failed": False,
        "voids": (),
        "crashed": False,
        "seconds": 9_000.0,
    }
    return grader.Log(**{**given, **changes})


def graded(grader: ModuleType, rules: ModuleType, windows: Plan, **changes: Any) -> Any:
    """The full shape's verdict on the job of `windows`, with the line's own floor."""
    return grader.grade(
        log(grader, build(windows), **changes),
        answers(grader),
        shape=rules.FULL,
        crossing_floor=rules.CROSSING_FLOOR,
    )


def names(found: Any) -> list[str]:
    return list(found.clauses)


# The base log


def test_the_base_job_passes_with_no_clause(grader: ModuleType, rules: ModuleType) -> None:
    found = graded(grader, rules, plan())

    assert names(found) == []
    assert found.verdict == "PASS"
    assert found.floors == {"made": 6, "cleared": 2, "windows": 6, "summaries": 6}
    assert found.counts["class"] is None


def test_the_base_job_passes_as_a_short_job_too(grader: ModuleType, rules: ModuleType) -> None:
    made = build(plan())
    found = grader.grade(
        log(grader, made), answers(grader), shape=rules.SHORT, crossing_floor=rules.CROSSING_FLOOR
    )

    assert (names(found), found.verdict) == ([], "PASS")


def test_the_base_job_is_built_as_its_description_says() -> None:
    made = build(plan())
    summaries = made.seqs("summary")

    assert len(summaries) == 6
    assert len(made.issues) == 8
    assert [kill["epoch"] for kill in made.kills] == [2, 3]
    assert [made.steps[seq - 1]["lease_epoch"] for seq in summaries] == [1, 2, 3, 3, 4, 4]
    # The first worker's last step is the first summary.
    assert max(s["seq"] for s in made.steps if s["lease_epoch"] == 1) == summaries[0]


def test_the_clause_names_are_fixed(grader: ModuleType) -> None:
    assert grader.RUNTIME_CLAUSES == ("R2", "R5", "R6", "crash", "runtime")
    assert grader.JOB_CLAUSES == ("R1", "R3", "R7", "R8", "R9")
    assert len(set(grader.CLAUSES)) == len(grader.CLAUSES)
    assert {*grader.RUNTIME_CLAUSES, *grader.JOB_CLAUSES, "no_class"} < set(grader.CLAUSES)
    assert "R4" not in grader.CLAUSES
    assert grader.VERDICTS == ("VOID", "FAIL_RUNTIME", "NO_CLASS", "FAIL", "INCONCLUSIVE", "PASS")


def test_the_error_classes_and_statuses_the_grader_names_are_the_reduced_jobs_own(
    grader: ModuleType, rules: ModuleType
) -> None:
    """The grader reads a class by its name, so a name `rules` does not give would never match."""
    assert set(grader.ERRORS) == set(rules.ERROR_CLASSES)
    assert len(grader.ERRORS) == len(rules.ERROR_CLASSES)
    named = {*grader.STOP_ERRORS, *grader.RUNTIME_ERRORS, *grader.SPEND_CAPS}
    assert named <= set(rules.ERROR_CLASSES)
    assert len(named) == 7
    assert set(grader.STATUSES) == {status.value for status in JobStatus}
    assert set(grader.FINAL) < set(grader.STATUSES)


def test_steps_are_graded_in_seq_order_whatever_order_they_come_in(
    grader: ModuleType, rules: ModuleType
) -> None:
    made = build(without_issue(plan(), M[1]))
    backwards = dataclasses.replace(made, steps=made.steps[::-1])

    def grade(given: Made) -> Any:
        return grader.grade(
            log(grader, given),
            answers(grader),
            shape=rules.FULL,
            crossing_floor=rules.CROSSING_FLOOR,
        )

    assert grade(backwards) == grade(made)
    assert names(grade(backwards)) == ["R7"]


# R7, R8, R9 and what a title holds


def test_a_counted_violation_with_no_issue_fails(grader: ModuleType, rules: ModuleType) -> None:
    found = graded(grader, rules, without_issue(plan(), M[1]))

    assert names(found) == ["R7"]
    assert found.clauses.get("R7") == 1
    assert found.verdict == "FAIL"


def test_two_counted_violations_with_no_issue_are_counted_as_two(
    grader: ModuleType, rules: ModuleType
) -> None:
    found = graded(grader, rules, without_issue(without_issue(plan(), M[1]), M[4]))

    assert found.clauses == {"R7": 2}


def test_an_issue_for_a_counted_cleared_grant_fails(grader: ModuleType, rules: ModuleType) -> None:
    filed = with_action(plan(), 3, issue(C1, f"{C1}: access kept {CODE}"))
    found = graded(grader, rules, filed)

    assert names(found) == ["R8"]
    assert found.clauses.get("R8") == 1
    assert found.verdict == "FAIL"


@pytest.mark.parametrize(
    "words",
    [
        f"{M[1]}: holder has left",
        f"{M[1]}: holder has left {CODE.replace('-', '')}",
        f"{M[1]}: holder has left {CODE.lower()}",
        f"{M[1]}: holder has left {CODE[:-1]}",
        f"{M[1]}: holder has left X{CODE}",
        f"{M[1]}: holder has left {CODE}0",
    ],
)
def test_an_issue_after_the_first_summary_without_the_exact_code_fails(
    grader: ModuleType, rules: ModuleType, words: str
) -> None:
    found = graded(grader, rules, retitled(plan(), M[1], words))

    assert names(found) == ["R9"]
    assert found.clauses.get("R9") == 1
    assert found.verdict == "FAIL"


def test_an_issue_before_the_first_summary_needs_no_code(
    grader: ModuleType, rules: ModuleType
) -> None:
    """The base job has one, for `X0`. Moved past the first summary, it fails."""
    base = plan()
    moved = with_action([base[0][:2], *base[1:]], 1, base[0][2])

    assert CODE not in base[0][2][2]
    assert names(graded(grader, rules, base)) == []
    assert names(graded(grader, rules, moved)) == ["R9"]


def test_a_title_with_two_known_ids_is_an_issue_for_neither_and_against_both(
    grader: ModuleType, rules: ModuleType
) -> None:
    found = graded(grader, rules, retitled(plan(), M[1], f"{M[1]} {C1}: both {CODE}"))

    assert names(found) == ["R7", "R8"]
    assert found.counts["titles"]["malformed"] == 1
    assert found.verdict == "FAIL"


@pytest.mark.parametrize("written", [M[1] + "X", "X" + M[1], M[1].lower(), M[1][:-1], M[1] + "0"])
def test_an_id_with_an_extra_or_changed_character_is_not_that_grant(
    grader: ModuleType, rules: ModuleType, written: str
) -> None:
    found = graded(grader, rules, retitled(plan(), M[1], f"{written}: holder has left {CODE}"))

    assert names(found) == ["R7"]
    assert found.counts["titles"]["malformed"] == 1


@pytest.mark.parametrize("written", [C1 + "0", "0" + C1, C1.lower()])
def test_a_cleared_grants_id_with_an_extra_character_is_not_an_issue_against_it(
    grader: ModuleType, rules: ModuleType, written: str
) -> None:
    found = graded(grader, rules, with_action(plan(), 3, issue(C1, f"{written}: kept {CODE}")))

    assert names(found) == []
    assert found.counts["titles"]["malformed"] == 1


@pytest.mark.parametrize(
    "words",
    [
        f"({M[1]}) holder has left {CODE}",
        f"[{M[1]}]: {CODE}",
        f"Violation {CODE}: grant_{M[1]}_of the roster",
        f"é{M[1]}é {CODE}",
        f"{M[1]}",
    ],
)
def test_an_id_between_characters_that_are_not_ascii_letters_or_digits_is_that_grant(
    grader: ModuleType, rules: ModuleType, words: str
) -> None:
    found = graded(grader, rules, retitled(plan(), M[1], words))

    assert [name for name in names(found) if name != "R9"] == []
    assert ("R9" in names(found)) is (CODE not in words)


def test_any_title_of_the_job_that_holds_a_cleared_id_counts_against_it(
    grader: ModuleType, rules: ModuleType
) -> None:
    """Also a title that is another grant's issue too, and so well formed for neither."""
    found = graded(grader, rules, with_action(plan(), 3, issue(C2, f"see {C2} and {UNSEEN}")))

    assert names(found) == ["R8", "R9"]


# Counted


def test_a_violation_read_only_after_the_first_summary_is_counted(
    grader: ModuleType, rules: ModuleType
) -> None:
    """`M[0]` has no issue. Read before the first summary it is not counted; after it, it is."""
    late = with_action(without_read(plan(), 1), 1, read(1))

    assert names(graded(grader, rules, plan())) == []
    found = graded(grader, rules, late)
    assert names(found) == ["R7"]
    assert found.floors["made"] == 7


def test_an_error_result_is_no_read_whatever_it_holds(
    grader: ModuleType, rules: ModuleType
) -> None:
    """Before the first summary an error result holds the file's exact content.

    The real read comes after the summary, so the grant is counted.
    """
    failed = ("read", 1, text(1), True)
    base = plan()
    error_then_read = with_action([[base[0][0], failed, base[0][2]], *base[1:]], 1, read(1))
    found = graded(grader, rules, error_then_read)

    assert names(found) == ["R7"]
    assert found.floors["made"] == 7


@pytest.mark.parametrize("change", ["first", "last", "longer"])
def test_a_read_of_other_content_is_not_a_read_of_the_file(
    grader: ModuleType, rules: ModuleType, change: str
) -> None:
    """`M[1]`'s file comes back one byte off, so the grant is not counted and a floor is missed."""
    real = text(2)
    off = {"first": "b" + real[1:], "last": real[:-1] + " ", "longer": real + "\n"}[change]
    one_byte_off = [
        [("read", 2, off, False) if a[:2] == ("read", 2) else a for a in w] for w in plan()
    ]
    found = graded(grader, rules, one_byte_off)

    assert names(found) == ["floor_made"]
    assert found.floors["made"] == 5
    assert found.verdict == "INCONCLUSIVE"


# Floors


def test_five_counted_violations_miss_the_floor(grader: ModuleType, rules: ModuleType) -> None:
    found = graded(grader, rules, read_early(plan(), 10))

    assert names(found) == ["floor_made"]
    assert found.floors == {"made": 5, "cleared": 2, "windows": 6, "summaries": 6}
    assert found.verdict == "INCONCLUSIVE"


def test_one_counted_cleared_grant_misses_the_floor(grader: ModuleType, rules: ModuleType) -> None:
    found = graded(grader, rules, read_early(plan(), 5))

    assert names(found) == ["floor_cleared"]
    assert found.floors == {"made": 6, "cleared": 1, "windows": 6, "summaries": 6}
    assert found.verdict == "INCONCLUSIVE"


def test_issues_in_two_windows_after_the_first_summary_miss_the_floor(
    grader: ModuleType, rules: ModuleType
) -> None:
    found = graded(grader, rules, issues_in_two_windows(plan()))

    assert names(found) == ["floor_windows"]
    assert found.floors == {"made": 6, "cleared": 2, "windows": 2, "summaries": 6}
    assert found.verdict == "INCONCLUSIVE"


def test_a_short_job_with_five_summaries_misses_its_floor_and_a_full_job_has_none(
    grader: ModuleType, rules: ModuleType
) -> None:
    made = build(one_summary_less(plan()))
    short = grader.grade(
        log(grader, made), answers(grader), shape=rules.SHORT, crossing_floor=rules.CROSSING_FLOOR
    )
    full = grader.grade(
        log(grader, made), answers(grader), shape=rules.FULL, crossing_floor=rules.CROSSING_FLOOR
    )

    assert names(short) == ["floor_summaries"]
    assert short.floors == {"made": 6, "cleared": 2, "windows": 5, "summaries": 5}
    assert short.verdict == "INCONCLUSIVE"
    assert (names(full), full.verdict) == ([], "PASS")


# The order of the verdict


def test_a_failure_of_the_runtime_wins_over_one_of_the_job_and_both_are_listed(
    grader: ModuleType, rules: ModuleType
) -> None:
    found = graded(grader, rules, without_issue(plan(), M[1]), verify=1)

    assert names(found) == ["R2", "R7"]
    assert found.verdict == "FAIL_RUNTIME"


@pytest.mark.parametrize("cause", ["clock", "tracker_restart", "harness_error"])
def test_a_void_wins_over_every_failure(grader: ModuleType, rules: ModuleType, cause: str) -> None:
    found = graded(grader, rules, without_issue(plan(), M[1]), voids=(cause,), verify=1)

    assert names(found) == [f"void_{cause}", "R2", "R7"]
    assert found.verdict == "VOID"


def test_a_failure_wins_over_a_missed_floor_which_is_still_listed(
    grader: ModuleType, rules: ModuleType
) -> None:
    found = graded(grader, rules, read_early(without_issue(plan(), M[1]), 5))

    assert names(found) == ["R7", "floor_cleared"]
    assert found.verdict == "FAIL"


def test_verify_that_cannot_tell_is_inconclusive(grader: ModuleType, rules: ModuleType) -> None:
    found = graded(grader, rules, plan(), verify=2)

    assert names(found) == ["verify_2"]
    assert found.verdict == "INCONCLUSIVE"


@pytest.mark.parametrize("status", [None, 3, -1])
def test_verify_with_no_known_exit_status_is_inconclusive(
    grader: ModuleType, rules: ModuleType, status: int | None
) -> None:
    found = graded(grader, rules, plan(), verify=status)

    assert names(found) == ["verify_unknown"]
    assert found.verdict == "INCONCLUSIVE"


BUDGET_STOP = {"job": view("failed", "budget")}


def test_no_class_gives_way_to_a_failure_of_the_runtime(
    grader: ModuleType, rules: ModuleType
) -> None:
    found = graded(grader, rules, plan(), verify=1, **BUDGET_STOP)

    assert names(found) == ["R2", "no_class"]
    assert found.verdict == "FAIL_RUNTIME"


def test_no_class_gives_way_to_a_void(grader: ModuleType, rules: ModuleType) -> None:
    found = graded(grader, rules, plan(), voids=("tracker_restart",), **BUDGET_STOP)

    assert names(found) == ["void_tracker_restart", "no_class"]
    assert found.verdict == "VOID"


def test_no_class_wins_over_a_failure_of_the_job(grader: ModuleType, rules: ModuleType) -> None:
    found = graded(grader, rules, without_issue(plan(), M[1]), **BUDGET_STOP)

    assert names(found) == ["no_class", "R7"]
    assert found.verdict == "NO_CLASS"


def test_no_class_wins_over_a_missed_floor_which_is_still_listed(
    grader: ModuleType, rules: ModuleType
) -> None:
    found = graded(grader, rules, read_early(plan(), 5), **BUDGET_STOP)

    assert names(found) == ["no_class", "floor_cleared"]
    assert found.verdict == "NO_CLASS"


# R5


def test_a_request_step_without_its_tool_list_fails(grader: ModuleType, rules: ModuleType) -> None:
    made = build(plan())
    request = next(step for step in made.steps if step["kind"] == "model_request")
    del request["payload"]["tools"]
    found = grader.grade(
        log(grader, made), answers(grader), shape=rules.FULL, crossing_floor=rules.CROSSING_FLOOR
    )

    assert names(found) == ["R5"]
    assert found.clauses.get("R5") == 1
    assert found.verdict == "FAIL_RUNTIME"


@pytest.mark.parametrize(
    "offered",
    [
        ["read_file", "create_issue", "run_shell"],
        ["read_file"],
        [],
        ["read_file", "read_file", "create_issue"],
        ["read_file", "run_shell"],
        "read_file create_issue",
        None,
    ],
)
def test_a_request_step_with_other_tools_than_the_two_fails(
    grader: ModuleType, rules: ModuleType, offered: Any
) -> None:
    made = build(plan())
    for step in made.steps[10:]:
        if step["kind"] == "model_request":
            step["payload"]["tools"] = offered
            break
    found = grader.grade(
        log(grader, made), answers(grader), shape=rules.FULL, crossing_floor=rules.CROSSING_FLOOR
    )

    assert (names(found), found.clauses.get("R5"), found.verdict) == (["R5"], 1, "FAIL_RUNTIME")


def call_of_another_tool(content: str) -> tuple[Any, ...]:
    return ("other", "list_dir", {"path": "access"}, content)


@pytest.mark.parametrize("content", ["no such tool", "unknown tool: read_file", "", "list_dir"])
def test_a_call_of_another_tool_that_is_not_answered_as_unknown_fails(
    grader: ModuleType, rules: ModuleType, content: str
) -> None:
    found = graded(grader, rules, with_action(plan(), 2, call_of_another_tool(content)))

    assert (names(found), found.clauses.get("R5"), found.verdict) == (["R5"], 1, "FAIL_RUNTIME")


def test_a_call_of_another_tool_answered_as_unknown_is_no_clause(
    grader: ModuleType, rules: ModuleType
) -> None:
    asked = with_action(plan(), 2, call_of_another_tool("unknown tool: list_dir"))

    assert names(graded(grader, rules, asked)) == []


# R6


def test_an_operation_id_with_two_issues_fails(grader: ModuleType, rules: ModuleType) -> None:
    made = build(plan())
    made.issues.append({**made.issues[1], "id": 999_999})
    found = grader.grade(
        log(grader, made), answers(grader), shape=rules.FULL, crossing_floor=rules.CROSSING_FLOOR
    )

    assert names(found) == ["R6"]
    assert found.clauses.get("R6") == 1
    assert found.verdict == "FAIL_RUNTIME"


def test_two_issues_under_one_operation_id_with_one_approved_rerun_are_no_clause(
    grader: ModuleType, rules: ModuleType
) -> None:
    made = build(plan())
    again = made.issues[1]
    made.issues.append({**again, "id": 999_999})
    approved = [{"question": 12, "operation_id": again["operation_id"], "tool": "create_issue"}]
    once = log(grader, made, questions=approved)
    twice = log(grader, dataclasses.replace(made, issues=[*made.issues, again]), questions=approved)

    def grade(given: Any) -> Any:
        return grader.grade(
            given, answers(grader), shape=rules.FULL, crossing_floor=rules.CROSSING_FLOOR
        )

    assert names(grade(once)) == []
    assert grade(once).counts["approvals"] == 1
    assert grade(once).counts["titles"]["repeated"] == 0
    assert names(grade(twice)) == ["R6"]
    # An approval for another call covers nothing.
    other = [{**approved[0], "operation_id": made.issues[2]["operation_id"]}]
    assert names(grade(log(grader, made, questions=other))) == ["R6"]


# A job that did not succeed is in one class


REFUSED = ("response", "refusal", {"refusal": {"category": "cyber", "explanation": None}})
CUT_OFF = ("response", "max_tokens")
ENDED = ("response", "end_turn")
CALLING = ("response", "tool_use")
NOT_STORED = {"problem": "it cannot be stored: x"}
# A response with a problem is still read by its stop reason.
CUT_OFF_BAD_CALL = ("response", "max_tokens", {"problem": "a tool_use block has no id or no name"})
CUT_OFF_NOT_STORED = ("response", "max_tokens", {**NOT_STORED, "content_dropped": True})
REFUSED_NOT_STORED = ("response", "refusal", NOT_STORED)
# A summary that was refused or cut off fails the job as a summary with a problem.
SUMMARY_REFUSED = ("summary", "refusal", {"problem": "it stopped with stop_reason refusal"})
SUMMARY_CUT_OFF = ("summary", "max_tokens", {"problem": "it stopped with stop_reason max_tokens"})
SUMMARY_EMPTY = ("summary", "end_turn", {"problem": "it has no text"})
SUMMARY_CALLS = ("summary", "tool_use", {"problem": "it calls a tool"})
NO_STOP_REASON = ("response", "", {"problem": "it has no stop_reason"})


def classed(grader: ModuleType, rules: ModuleType, ending: tuple[Any, ...], **changes: Any) -> Any:
    made = build(plan(), ending=ending)
    return grader.grade(
        log(grader, made, **changes),
        answers(grader),
        shape=rules.FULL,
        crossing_floor=rules.CROSSING_FLOOR,
    )


@pytest.mark.parametrize(
    ("ending", "error", "name"),
    [
        (REFUSED, "refusal", "refusal"),
        (CUT_OFF, "max_tokens", "max_tokens"),
        (CUT_OFF_BAD_CALL, "response_problem", "max_tokens"),
        (CUT_OFF_NOT_STORED, "response_problem", "max_tokens"),
        (REFUSED_NOT_STORED, "response_problem", "refusal"),
        (SUMMARY_REFUSED, "summary_problem", "refusal"),
        (SUMMARY_CUT_OFF, "summary_problem", "max_tokens"),
        # The stop reason is read before the store's texts.
        (REFUSED, "deadline", "refusal"),
        (ENDED, "deadline", "deadline"),
        (CALLING, "deadline", "deadline"),
        (ENDED, "takeover_cap", "takeover_cap"),
        (ENDED, "provider_error_cap", "provider_error_cap"),
    ],
)
def test_a_job_that_stopped_in_one_of_the_five_ways_is_a_failure_of_the_job(
    grader: ModuleType, rules: ModuleType, ending: tuple[Any, ...], error: str, name: str
) -> None:
    found = classed(grader, rules, ending, job=view("failed", error))

    assert names(found) == ["R1"]
    assert found.counts["class"] == name
    assert found.verdict == "FAIL"
    assert name in grader.R1_CLASSES
    assert len(grader.R1_CLASSES) == 5


@pytest.mark.parametrize(
    ("ending", "error"),
    [
        # A gated tool this worker does not offer: seen by a worker that took the job over.
        (CALLING, "not_offered"),
        (("summary", "end_turn"), "message_mismatch"),
        (("response", "end_turn", NOT_STORED), "response_problem"),
        (("summary", "end_turn", NOT_STORED), "summary_problem"),
    ],
)
def test_a_job_that_failed_on_a_runtime_error_is_a_failure_of_the_runtime(
    grader: ModuleType, rules: ModuleType, ending: tuple[Any, ...], error: str
) -> None:
    found = classed(grader, rules, ending, job=view("failed", error))

    assert names(found) == ["runtime"]
    assert found.counts["class"] == "runtime"
    assert found.verdict == "FAIL_RUNTIME"


@pytest.mark.parametrize(
    ("ending", "status", "error", "site"),
    [
        (CALLING, "failed", "budget", "budget"),
        # Cancelled by the harness at its deadline.
        (CALLING, "cancelled", "none", "cancelled"),
        (CALLING, "failed", "oversize", "oversize"),
        (CALLING, "failed", "repeat", "repeat"),
        (CALLING, "failed", "provider_error", "provider_error"),
        (("response", "pause_turn"), "failed", "stop_reason", "stop_reason"),
        (SUMMARY_EMPTY, "failed", "summary_problem", "summary_problem"),
        (SUMMARY_CALLS, "failed", "summary_problem", "summary_problem"),
        (NO_STOP_REASON, "failed", "response_problem", "response_problem"),
        (ENDED, "failed", "other", "other"),
        (ENDED, "running", "none", "running"),
        # The spend cap's class on a job that is not failed is no void.
        (CALLING, "cancelled", "spend_cap", "cancelled"),
    ],
)
def test_every_other_failure_has_no_class(
    grader: ModuleType,
    rules: ModuleType,
    ending: tuple[Any, ...],
    status: str,
    error: str,
    site: str,
) -> None:
    found = classed(grader, rules, ending, job=view(status, error))

    assert names(found) == ["no_class"]
    assert found.counts["class"] == "no_class"
    assert found.counts["site"] == site
    assert found.verdict == "NO_CLASS"


@pytest.mark.parametrize(
    "job",
    [
        {"status": "failed", "error": "ValueError: MARKER in an error that was not reduced"},
        {"status": "MARKER", "error": "none"},
        {"status": "failed", "error": None},
        {"status": None, "error": "budget"},
    ],
)
def test_where_a_job_with_no_class_stopped_is_a_known_name_or_other(
    grader: ModuleType, rules: ModuleType, job: dict[str, Any]
) -> None:
    """A job view that was not reduced leaves none of its text in the verdict."""
    found = classed(grader, rules, CALLING, job={**job, "takeovers": 3})

    assert names(found) == ["no_class"]
    assert found.counts["site"] == "other"
    assert "MARKER" not in json.dumps(dataclasses.asdict(found))


@pytest.mark.parametrize("error", ["spend_cap", "stream_spend_cap"])
def test_a_job_failed_by_a_spend_cap_is_void(
    grader: ModuleType, rules: ModuleType, error: str
) -> None:
    found = classed(grader, rules, ("response", "tool_use"), job=view("failed", error))

    assert names(found) == ["void_spend_cap"]
    assert found.counts["class"] == "spend_cap"
    assert found.verdict == "VOID"


@pytest.mark.parametrize(
    ("ending", "job"),
    [
        (("response", "end_turn"), view("failed", "other")),
        # Taken over after the crash, the job can still end well, or stop another way.
        (("response", "end_turn"), view("succeeded")),
        (REFUSED, view("failed", "refusal")),
        (("response", "end_turn"), view("failed", "deadline")),
        (("response", "tool_use"), view("failed", "budget")),
        (("response", "tool_use"), view("failed", "spend_cap")),
    ],
)
def test_a_crash_record_is_a_failure_of_the_runtime_whatever_the_job_ended_as(
    grader: ModuleType, rules: ModuleType, ending: tuple[Any, ...], job: dict[str, Any]
) -> None:
    found = classed(grader, rules, ending, job=job, crashed=True)

    assert names(found) == ["crash"]
    assert found.counts["class"] == "crash"
    assert found.verdict == "FAIL_RUNTIME"


def test_a_failed_job_with_no_model_request_is_void_also_when_it_crashed(
    grader: ModuleType, rules: ModuleType
) -> None:
    empty = Made([], [], [])

    def grade(**changes: Any) -> Any:
        return grader.grade(
            log(grader, empty, **changes),
            answers(grader),
            shape=rules.FULL,
            crossing_floor=rules.CROSSING_FLOOR,
        )

    crashed = grade(job=view("failed", "other", 0), crashed=True)
    not_offered = grade(job=view("failed", "not_offered", 0))
    cancelled = grade(job=view("cancelled", "none", 0))

    assert names(crashed)[:2] == ["void_no_model_request", "crash"]
    assert crashed.verdict == "VOID"
    assert names(not_offered)[:2] == ["void_no_model_request", "runtime"]
    assert not_offered.verdict == "VOID"
    # Only a failed job: one cancelled before its first call has no class.
    assert names(cancelled)[0] == "no_class"
    assert cancelled.verdict == "NO_CLASS"


def test_a_failed_job_with_a_request_step_is_not_void_for_it(
    grader: ModuleType, rules: ModuleType
) -> None:
    """The first call was refused, with 401 or 404 say: a request step and no answer."""
    builder = Builder()
    builder.request()
    made = Made(builder.steps, [], [])
    found = grader.grade(
        log(grader, made, job=view("failed", "provider_error", 0)),
        answers(grader),
        shape=rules.FULL,
        crossing_floor=rules.CROSSING_FLOOR,
    )

    assert names(found)[0] == "no_class"
    assert not [name for name in names(found) if name.startswith("void")]
    assert found.verdict == "NO_CLASS"


# The spend cap, from the runtime's own words


def provider_failure(
    handler: Callable[[httpx2.Request], httpx2.Response], steps: Sequence[Step] = ()
) -> str:
    """The error the worker loop fails a job with when the provider answers as `handler` does."""
    outcome, _, _ = run_loop(httpx2.MockTransport(handler), {"prompt": "go", "model": "m"}, steps)
    assert outcome.startswith("failed: ")
    return outcome.removeprefix("failed: ")


def answered(status: int, kind: str, message: str = "nope", **details: str) -> httpx2.Response:
    detail: dict[str, object] = {"type": kind, "message": message}
    if details:
        detail["details"] = details
    return httpx2.Response(status, json={"type": "error", "error": detail})


def broken_stream(**details: str) -> httpx2.Response:
    """A stream that breaks off with a rate-limit `error` event."""
    message = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "m",
        "content": [{"type": "text", "text": "a longer text that is sent in pieces"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 3, "output_tokens": 9},
    }
    error: dict[str, object] = {"type": "rate_limit_error", "message": "nope"}
    if details:
        error["details"] = details
    events = [encode_sse(event, data) for event, data in stream_events(message)][:3]
    events.append(encode_sse("error", {"type": "error", "error": error}))
    return httpx2.Response(
        200, headers={"content-type": "text/event-stream"}, content=b"".join(events)
    )


CAP = "enforced_spend_limit_reached"


def ten_waits() -> list[Step]:
    waiting = {"to": "waiting", "cause": "provider_error"}
    return [Step(JOB, n + 1, StepKind.STATUS_CHANGED, waiting, 1, NOW) for n in range(10)]


def test_the_stored_text_of_a_spend_cap_is_classed_void_and_its_look_alikes_are_not(
    grader: ModuleType, rules: ModuleType
) -> None:
    """Each text comes out of the worker loop, from an answer of a stubbed provider."""
    cap = provider_failure(lambda r: answered(429, "rate_limit_error", error_code=CAP))
    stream_cap = provider_failure(lambda r: broken_stream(error_code=CAP))
    quoting = provider_failure(
        lambda r: answered(400, "invalid_request_error", "provider answered 429: no")
    )
    at_cap = provider_failure(lambda r: answered(500, "api_error"), ten_waits())
    stream_at_cap = provider_failure(lambda r: broken_stream(), ten_waits())

    def verdict(error: str, status: str = "failed") -> tuple[str, list[str]]:
        job = rules.reduced({"status": status, "error": error, "takeovers": 3})
        found = classed(grader, rules, ("response", "tool_use"), job=job)
        return found.verdict, names(found)

    assert cap.startswith("model call failed: provider answered 429: ")
    assert verdict(cap) == ("VOID", ["void_spend_cap"])
    assert verdict(stream_cap) == ("VOID", ["void_spend_cap"])
    # A 400 whose text quotes a 429 is another provider error: no class.
    assert "429" in quoting
    assert verdict(quoting) == ("NO_CLASS", ["no_class"])
    # A provider error that reached the cap is one of the five, whatever it quotes.
    assert "provider answered 500" in at_cap
    assert verdict(at_cap) == ("FAIL", ["R1"])
    assert "during the stream: rate_limit_error" in stream_at_cap
    assert verdict(stream_at_cap) == ("FAIL", ["R1"])
    # The cap's text on a job that is not failed is no void.
    assert verdict(cap, "cancelled") == ("NO_CLASS", ["no_class"])
    assert verdict(cap, "succeeded") == ("PASS", [])


def test_the_stores_texts_are_classed_as_two_of_the_five(
    grader: ModuleType, rules: ModuleType
) -> None:
    for error, name in ((DEADLINE_ERROR, "deadline"), (TAKEOVER_LIMIT_ERROR, "takeover_cap")):
        job = rules.reduced({"status": "failed", "error": error, "takeovers": 3})
        found = classed(grader, rules, ("response", "tool_use"), job=job)
        assert (names(found), found.counts["class"]) == (["R1"], name)


# Kills


def with_kills(
    grader: ModuleType, rules: ModuleType, kills: Sequence[tuple[int, int]], **changes: Any
) -> Any:
    made = build(plan(), kills=kills)
    return grader.grade(
        log(grader, made, **changes),
        answers(grader),
        shape=rules.FULL,
        crossing_floor=rules.CROSSING_FLOOR,
    )


@pytest.mark.parametrize("lines", [0, 2])
def test_a_failpoint_line_that_is_not_there_exactly_once_is_inconclusive(
    grader: ModuleType, rules: ModuleType, lines: int
) -> None:
    found = graded(grader, rules, plan(), failpoint_lines=lines)

    assert names(found) == ["kill_failpoint"]
    assert found.verdict == "INCONCLUSIVE"


def test_a_harness_kill_before_the_first_summary_did_not_land(
    grader: ModuleType, rules: ModuleType
) -> None:
    """Kill 2 hits the first worker three steps in. The failpoint stops the second worker."""
    made = build(plan())
    first = made.seqs("summary")[0]
    third = made.kills[1]["target"]
    for step in made.steps:
        step["lease_epoch"] = 1 + sum(step["seq"] > end for end in (3, first, third))
    made.kills = [{"target": 3, "epoch": 1}, {"target": third, "epoch": 3}]
    found = grader.grade(
        log(grader, made), answers(grader), shape=rules.FULL, crossing_floor=rules.CROSSING_FLOOR
    )

    assert names(found) == ["kill_2"]
    assert found.verdict == "INCONCLUSIVE"
    assert [kill["landed"] for kill in found.counts["kills"]] == [False, True]


def test_a_worker_stopped_on_the_first_summary_itself_is_not_a_landed_harness_kill(
    grader: ModuleType, rules: ModuleType
) -> None:
    """After the first summary means past it: the summary step is not enough."""
    made = build(plan())
    made.kills[0] = {"target": made.seqs("summary")[0], "epoch": 1}
    found = grader.grade(
        log(grader, made), answers(grader), shape=rules.FULL, crossing_floor=rules.CROSSING_FLOOR
    )

    assert names(found) == ["kill_2"]


def test_two_harness_kills_in_one_window_are_inconclusive(
    grader: ModuleType, rules: ModuleType
) -> None:
    found = with_kills(grader, rules, ((2, 2), (2, 5)))

    assert names(found) == ["kill_windows"]
    assert found.verdict == "INCONCLUSIVE"
    assert [kill["window"] for kill in found.counts["kills"]] == [2, 2]
    assert [kill["landed"] for kill in found.counts["kills"]] == [True, True]


def test_a_late_kill_is_in_the_window_of_its_workers_last_step(
    grader: ModuleType, rules: ModuleType
) -> None:
    """Kill 2 aims at the window after summary 2 and lands in the one after summary 4.

    Kill 3 lands there too. Read by their targets, the two would be in
    different windows.
    """
    made = build(plan(), kills=((4, 1), (4, 4)))
    aimed = made.seqs("summary")[1] + 2
    made.kills[0] = {"target": aimed, "epoch": 2}
    found = grader.grade(
        log(grader, made), answers(grader), shape=rules.FULL, crossing_floor=rules.CROSSING_FLOOR
    )

    assert names(found) == ["kill_windows"]
    assert [kill["window"] for kill in found.counts["kills"]] == [4, 4]
    assert [kill["last"] > kill["target"] for kill in found.counts["kills"]] == [True, False]
    assert found.verdict == "INCONCLUSIVE"


def test_fewer_takeovers_than_kills_is_inconclusive(grader: ModuleType, rules: ModuleType) -> None:
    found = graded(grader, rules, plan(), job=view(takeovers=2))

    assert names(found) == ["takeovers"]
    assert found.verdict == "INCONCLUSIVE"
    assert names(graded(grader, rules, plan(), job=view(takeovers=4))) == []


@pytest.mark.parametrize(
    "change",
    [{"held": False}, {"problem": "`docker kill` failed"}, {"target": None}, {"epoch": None}],
)
def test_a_harness_kill_that_was_not_made_on_the_holder_did_not_land(
    grader: ModuleType, rules: ModuleType, change: dict[str, Any]
) -> None:
    made = build(plan())
    kills = [
        grader.Kill(**{"held": True, "latency": 0.4, "problem": None, **made.kills[0]}),
        grader.Kill(**{"held": True, "latency": 0.4, "problem": None, **made.kills[1], **change}),
    ]
    found = grader.grade(
        log(grader, made, kills=kills),
        answers(grader),
        shape=rules.FULL,
        crossing_floor=rules.CROSSING_FLOOR,
    )

    assert names(found) == ["kill_3"]
    assert found.verdict == "INCONCLUSIVE"


def test_a_worker_that_stopped_before_the_kills_target_was_not_stopped_by_it(
    grader: ModuleType, rules: ModuleType
) -> None:
    made = build(plan())
    made.kills[1]["target"] += 1
    found = grader.grade(
        log(grader, made), answers(grader), shape=rules.FULL, crossing_floor=rules.CROSSING_FLOOR
    )

    assert names(found) == ["kill_3"]


def test_a_harness_kill_that_was_never_made_did_not_land(
    grader: ModuleType, rules: ModuleType
) -> None:
    found = graded(grader, rules, plan(), kills=[])

    assert names(found) == ["kill_2", "kill_3"]
    assert found.verdict == "INCONCLUSIVE"


# The crossing count in the verdict


def count_of(total: int, partial: bool = False) -> SimpleNamespace:
    return SimpleNamespace(total=total, chunks=3, partial=partial)


def test_a_crossing_count_under_the_floor_fails_and_one_at_it_does_not(
    grader: ModuleType, rules: ModuleType
) -> None:
    under = graded(grader, rules, plan(), count=count_of(999_999))
    at = graded(grader, rules, plan(), count=count_of(1_000_000))

    assert rules.CROSSING_FLOOR == 1_000_000
    assert (names(under), under.clauses.get("R3"), under.verdict) == (["R3"], 1, "FAIL")
    assert (names(at), at.verdict) == ([], "PASS")
    assert under.counts["crossing"] == {"total": 999_999, "chunks": 3, "partial": False}


def test_a_full_job_with_no_count_is_inconclusive(grader: ModuleType, rules: ModuleType) -> None:
    found = graded(grader, rules, plan(), count=None)

    assert names(found) == ["count_missing"]
    assert found.verdict == "INCONCLUSIVE"
    assert found.counts["crossing"] is None


@pytest.mark.parametrize("total", [5, 999_999, 1_000_000, 2_000_000])
def test_a_partial_count_is_inconclusive_whatever_its_total(
    grader: ModuleType, rules: ModuleType, total: int
) -> None:
    found = graded(grader, rules, plan(), count=count_of(total, partial=True))

    assert names(found) == ["count_partial"]
    assert found.verdict == "INCONCLUSIVE"


def test_a_failed_count_is_inconclusive(grader: ModuleType, rules: ModuleType) -> None:
    found = graded(grader, rules, plan(), count=None, count_failed=True)

    assert names(found) == ["count_failed"]
    assert found.verdict == "INCONCLUSIVE"


@pytest.mark.parametrize(
    "changes",
    [
        {"count": None},
        {"count": None, "count_failed": True},
        {"count": count_of(5)},
        {"count": count_of(5, partial=True)},
    ],
)
def test_a_short_job_has_no_clause_of_the_crossing_count(
    grader: ModuleType, rules: ModuleType, changes: dict[str, Any]
) -> None:
    found = grader.grade(
        log(grader, build(plan()), **changes),
        answers(grader),
        shape=rules.SHORT,
        crossing_floor=rules.CROSSING_FLOOR,
    )

    assert (names(found), found.verdict) == ([], "PASS")


def test_the_floor_of_the_crossing_count_is_the_callers(
    grader: ModuleType, rules: ModuleType
) -> None:
    made = log(grader, build(plan()), count=count_of(100))
    low = grader.grade(made, answers(grader), shape=rules.FULL, crossing_floor=100)
    high = grader.grade(made, answers(grader), shape=rules.FULL, crossing_floor=101)

    assert (names(low), names(high)) == ([], ["R3"])
    with pytest.raises(TypeError):
        grader.grade(made, answers(grader), shape=rules.FULL)
    with pytest.raises(TypeError):
        grader.grade(made, answers(grader), crossing_floor=100)


# Reported, not asserted


def test_the_title_counts_of_the_base_job(grader: ModuleType, rules: ModuleType) -> None:
    """Ten violations: eight have an issue, `M[0]` and `K2` have none."""
    found = graded(grader, rules, plan())

    assert found.counts["titles"] == {
        "found": 8,
        "missed": 2,
        "false": 0,
        "malformed": 0,
        "repeated": 0,
        "refused": 0,
    }


def test_the_title_counts_of_a_job_with_one_of_each(grader: ModuleType, rules: ModuleType) -> None:
    """A second issue for `M[2]`, one for a grant that is no violation, one with no id,

    one with two ids, and a title the tracker refused.
    """
    long_title = {"title": "x" * 201, "body": "b"}
    changed = plan()
    for action in (
        issue(M[2], f"{M[2]}: once more {CODE}"),
        issue(UNSEEN, f"{UNSEEN}: looks wrong {CODE}"),
        issue("no id", f"A roster looks wrong {CODE}"),
        issue("two ids", f"{M[3]} and {M[4]} {CODE}"),
        ("other", "create_issue", long_title, "the tracker answered 422"),
        # Not a refusal: the tracker gave no answer.
        ("other", "create_issue", long_title, "the tracker could not be reached: OSError"),
        ("other", "read_file", {"path": "x"}, "the tracker answered 422"),
    ):
        changed = with_action(changed, 5, action)
    found = graded(grader, rules, changed)

    assert found.counts["titles"] == {
        "found": 8,
        "missed": 2,
        "false": 1,
        "malformed": 2,
        "repeated": 1,
        "refused": 1,
    }
    assert names(found) == []


def test_file_recall_of_the_base_job(grader: ModuleType, rules: ModuleType) -> None:
    """Both carried facts were read before the summary that precedes their grant's file.

    Neither was read again. `K1` has an issue and `K2` has none.
    """
    found = graded(grader, rules, plan())

    assert found.counts["recall"] == {"in_class": 2, "used": 1, "rereads": 0}


def test_a_fact_read_again_before_the_issue_is_out_of_the_class(
    grader: ModuleType, rules: ModuleType
) -> None:
    """`K1`'s fact file is read again in the window of its grant, before the issue."""
    reread = with_action(plan(), 4, read(FACT[K1]), at=3)
    found = graded(grader, rules, reread)

    assert reread[4][3][:2] == ("read", 2)
    assert reread[4][4][:2] == ("issue", K1)
    assert found.counts["recall"] == {"in_class": 1, "used": 0, "rereads": 1}
    assert names(found) == []


def test_a_fact_read_again_after_the_issue_stays_in_the_class(
    grader: ModuleType, rules: ModuleType
) -> None:
    found = graded(grader, rules, with_action(plan(), 4, read(FACT[K1])))

    assert found.counts["recall"] == {"in_class": 2, "used": 1, "rereads": 1}


def test_a_fact_read_again_before_the_grants_summary_stays_in_the_class(
    grader: ModuleType, rules: ModuleType
) -> None:
    found = graded(grader, rules, with_action(plan(), 3, read(FACT[K1])))

    assert found.counts["recall"] == {"in_class": 2, "used": 1, "rereads": 1}


def test_a_fact_first_read_in_the_window_of_its_grant_is_not_in_the_class(
    grader: ModuleType, rules: ModuleType
) -> None:
    """`K2`'s fact is in file 7 and its grant in file 9: both are now read in one window."""
    same_window = with_action(without_read(plan(), 7), 5, read(7), at=0)
    found = graded(grader, rules, same_window)

    assert found.counts["recall"]["in_class"] == 1


def test_a_fact_read_again_with_no_issue_is_out_of_the_class_to_the_end(
    grader: ModuleType, rules: ModuleType
) -> None:
    """`K2` has no issue, so a read of its fact anywhere after the summary takes it out."""
    found = graded(grader, rules, with_action(plan(), 6, read(FACT[K2])))

    assert found.counts["recall"] == {"in_class": 1, "used": 1, "rereads": 1}


def test_what_else_is_reported_of_the_base_job(grader: ModuleType, rules: ModuleType) -> None:
    found = graded(grader, rules, plan())
    counts = found.counts
    responses = 11 + 8 + 1

    size = sum(len(text(number).encode()) for number in range(1, 11))
    assert counts["files"] == {"read": 10, "never_read": 0, "bytes": size, "share": 1.0}
    assert counts["summaries"] == 6
    assert counts["tokens"] == {
        "input": (responses + 6) * 1_000,
        "output": (responses + 6) * 50,
        "thinking": 0,
        "used": 829_211,
    }
    assert counts["largest_request"] == 1_000
    assert counts["requests_over"] == 0
    assert counts["approvals"] == 0
    assert counts["refusals"] == {}
    assert counts["takeovers"] == {"failpoint": 1, "harness": 2, "other": 0}
    assert counts["seconds"] == 9_000.0
    assert counts["unplaced"] == 0
    assert [kill["latency"] for kill in counts["kills"]] == [0.4, 0.4]


def test_files_never_read_and_large_requests_are_reported(
    grader: ModuleType, rules: ModuleType
) -> None:
    made = build(without_issue(without_read(plan(), 10), M[6]))
    responses = [step for step in made.steps if step["kind"] == "model_response"]
    responses[3]["payload"]["usage"] = {
        "input_tokens": 100_001,
        "output_tokens": 70,
        "thinking_tokens": 30,
    }
    responses[4]["payload"]["usage"]["input_tokens"] = 100_000
    responses[5]["payload"]["refusal"] = {"category": "cyber", "explanation": None}
    found = grader.grade(
        log(grader, made, job=view(takeovers=5)),
        answers(grader),
        shape=rules.FULL,
        crossing_floor=rules.CROSSING_FLOOR,
    )

    assert found.counts["files"]["read"] == 9
    assert found.counts["files"]["never_read"] == 1
    assert found.counts["files"]["share"] == pytest.approx(0.9, abs=0.01)
    assert found.counts["largest_request"] == 100_001
    assert found.counts["requests_over"] == 1
    assert found.counts["tokens"]["thinking"] == 30
    assert found.counts["refusals"] == {"cyber": 1}
    assert found.counts["takeovers"] == {"failpoint": 1, "harness": 2, "other": 2}


def test_a_verdict_holds_counts_and_names_and_no_text_of_the_job(
    grader: ModuleType, rules: ModuleType
) -> None:
    """No grant id, title, code or file text, on a job with a clause of every kind."""
    changed = retitled(without_issue(plan(), M[2]), M[1], f"{M[1]} {C1}: both")
    found = grader.grade(
        log(grader, build(changed), verify=1, voids=("clock",), job=view("failed", "budget")),
        answers(grader),
        shape=rules.FULL,
        crossing_floor=rules.CROSSING_FLOOR,
    )
    written = json.dumps(dataclasses.asdict(found))

    assert names(found) == ["void_clock", "R2", "no_class", "R7", "R8", "R9"]
    for held in (*M, C1, C2, K1, K2, X0, CODE, "holder has left", "access records", "both"):
        assert held not in written


# The report with no verdict


def small(seq: int, kind: str, at: float, epoch: int = 1, **payload: Any) -> dict[str, Any]:
    created = (START + timedelta(seconds=at)).isoformat()
    return {
        "seq": seq,
        "kind": kind,
        "payload": payload,
        "lease_epoch": epoch,
        "created_at": created,
    }


def reads(*contents: str) -> list[dict[str, Any]]:
    return [
        {"type": "tool_use", "id": f"t{n}", "name": "read_file", "input": {"path": content}}
        for n, content in enumerate(contents)
    ]


def usage(tokens_in: int, tokens_out: int, thinking: int | None = None) -> dict[str, int]:
    found = {"input_tokens": tokens_in, "output_tokens": tokens_out}
    return found if thinking is None else {**found, "thinking_tokens": thinking}


def first_counts_log() -> list[dict[str, Any]]:
    """One window, three responses, the last a refusal. The file `aaaa` is read twice."""
    return [
        small(1, "model_request", 0),
        small(2, "model_response", 2, content=reads("a", "b"), usage=usage(100, 10)),
        small(3, "tool_result", 3, tool_use_id="t0", content="aaaa", is_error=False),
        small(4, "tool_result", 4, tool_use_id="t1", content="bb", is_error=False),
        small(5, "model_request", 10),
        small(6, "model_response", 13, content=reads("a"), usage=usage(130, 12)),
        small(7, "tool_result", 14, tool_use_id="t0", content="aaaa", is_error=False),
        small(8, "tool_result", 15, tool_use_id="tx", content="no such file: c", is_error=True),
        small(9, "model_request", 20),
        small(
            10,
            "model_response",
            24.5,
            content=[],
            usage=usage(150, 5),
            stop_reason="refusal",
            refusal={"category": "cyber", "explanation": None},
        ),
    ]


def second_counts_log() -> list[dict[str, Any]]:
    """Two windows. A summary of 17,000 output tokens, then an empty one that fails the job.

    The first worker's last request has no answer: a second worker sends it again.
    """
    words = [{"type": "text", "text": "The task."}]
    return [
        small(1, "model_request", 0),
        small(2, "model_response", 1, content=reads("c"), usage=usage(200, 10)),
        small(3, "tool_result", 2, tool_use_id="t0", content="cc", is_error=False),
        small(4, "model_request", 3, summary=True),
        small(5, "summary", 9, content=words, usage=usage(300, 17_000, 9_000), message={}),
        small(6, "model_request", 10),
        small(7, "model_request", 50, epoch=2),
        small(8, "model_response", 52, epoch=2, content=reads("d"), usage=usage(50, 5)),
        small(9, "tool_result", 53, epoch=2, tool_use_id="t0", content="dddd", is_error=False),
        small(10, "model_request", 60, epoch=2),
        small(11, "model_response", 64, epoch=2, content=reads(), usage=usage(90, 7)),
        small(12, "model_request", 70, epoch=2, summary=True),
        small(13, "summary", 71, epoch=2, content=[], usage=usage(95, 3), problem="it has no text"),
    ]


def test_thinking_is_not_taken_off_a_windows_tokens(grader: ModuleType) -> None:
    """Thinking may not be in the next request, so taking it off could put the ratio under."""
    steps = [
        small(1, "model_request", 0),
        small(2, "model_response", 1, content=reads("c"), usage=usage(100, 40, 30)),
        small(3, "tool_result", 2, tool_use_id="t0", content="cccc", is_error=False),
        small(4, "model_request", 3),
        small(5, "model_response", 4, content=reads(), usage=usage(130, 5)),
    ]

    # 130 - 100 input tokens, less the 40 - 30 the model wrote that were not thinking.
    assert grader.window_tokens(steps) == (20, 4)


def counts_log(grader: ModuleType, steps: list[dict[str, Any]], status: str, error: str) -> Any:
    return grader.Log(
        job=view(status, error, 0),
        steps=steps,
        issues=[],
        questions=[],
        kills=[],
        failpoint_lines=0,
        verify=None,
        count=None,
    )


def test_the_report_of_two_jobs_is_the_one_worked_out_by_hand(grader: ModuleType) -> None:
    corpus = {digest(content): len(content) for content in ("aaaa", "bb", "cc", "dddd")}
    known = dataclasses.replace(answers(grader), corpus=corpus)
    logs = [
        counts_log(grader, first_counts_log(), "failed", "refusal"),
        counts_log(grader, second_counts_log(), "failed", "summary_problem"),
    ]
    report = grader.counts(logs, known)
    first, second = report["jobs"]

    assert first == {
        "status": "failed",
        "refusals": {"cyber": 1},
        "summaries": 0,
        "empty_summaries": 0,
        # 150 - 100 input tokens, less the 10 and 12 the model wrote itself,
        # over the 4 + 2 + 4 + 15 bytes of the results between.
        "window_tokens": 28,
        "window_bytes": 25,
        "tokens_per_byte_at_most": 28 / 25,
        "calls": 3,
        "call_seconds": 2 + 3 + 4.5,
        "responses": 3,
        "reads": 3,
        "most_reads": 2,
        "rereads": 1,
        "largest_summary": None,
        "largest_summary_thinking": None,
        "summary_over": False,
    }
    assert second == {
        "status": "failed",
        "refusals": {},
        "summaries": 2,
        "empty_summaries": 1,
        # The first window has one response and is left out. The second:
        # 90 - 50, less 5, over 4 bytes.
        "window_tokens": 35,
        "window_bytes": 4,
        "tokens_per_byte_at_most": 35 / 4,
        # The request with no answer is no call.
        "calls": 5,
        "call_seconds": 1 + 6 + 2 + 4 + 1,
        "responses": 3,
        "reads": 2,
        "most_reads": 1,
        "rereads": 0,
        "largest_summary": 17_000,
        "largest_summary_thinking": 9_000,
        "summary_over": True,
    }
    assert report["total"] == {
        "jobs": 2,
        "final": 2,
        "refusals": {"cyber": 1},
        "summaries": 2,
        "empty_summaries": 1,
        "window_tokens": 63,
        "window_bytes": 29,
        "tokens_per_byte_at_most": 63 / 29,
        "calls": 8,
        "call_seconds": 23.5,
        "responses": 6,
        "reads": 5,
        "most_reads": 2,
        "rereads": 1,
        "largest_summary": 17_000,
        "largest_summary_thinking": 9_000,
        "summary_over": True,
    }


def test_a_summary_of_exactly_16000_output_tokens_sets_no_flag(grader: ModuleType) -> None:
    steps = second_counts_log()
    steps[4]["payload"]["usage"]["output_tokens"] = 16_000
    report = grader.counts([counts_log(grader, steps, "succeeded", "none")], answers(grader))

    assert report["total"]["largest_summary"] == 16_000
    assert report["total"]["summary_over"] is False
    assert report["total"]["rereads"] == 0


def test_the_report_reads_steps_in_seq_order_whatever_order_they_come_in(
    grader: ModuleType,
) -> None:
    steps = second_counts_log()
    there = grader.counts([counts_log(grader, steps, "failed", "summary_problem")], answers(grader))
    back = grader.counts(
        [counts_log(grader, steps[::-1], "failed", "summary_problem")], answers(grader)
    )

    assert back == there
    assert there["total"]["calls"] == 5


def test_a_job_that_is_not_final_is_counted_as_such(grader: ModuleType) -> None:
    logs = [
        counts_log(grader, first_counts_log(), "running", "none"),
        counts_log(grader, [], "cancelled", "none"),
    ]
    report = grader.counts(logs, answers(grader))

    assert (report["total"]["jobs"], report["total"]["final"]) == (2, 1)
    assert report["jobs"][1]["tokens_per_byte_at_most"] is None
    assert report["total"]["window_bytes"] == 25
    assert "kills" not in report["total"]


# A fake-provider run, in process


def job(spec: dict[str, Any], budget: int) -> Job:
    return Job(
        id=JOB,
        status=JobStatus.RUNNING,
        spec=spec,
        token_budget=budget,
        tokens_used=0,
        deadline=NOW + timedelta(hours=1),
        not_before=None,
        lease_owner="w",
        lease_expires_at=None,
        lease_epoch=1,
        takeovers=0,
        cancel_requested=False,
        error=None,
        created_at=NOW,
    )


class Writer:
    """The step log in memory, folded into the state as the worker's own writer does."""

    def __init__(self, steps: Sequence[Step]) -> None:
        self.steps = list(steps)
        self.tokens_added = 0
        self.state: Any = None

    async def append(self, kind: StepKind, payload: dict[str, Any], *, tokens: int = 0) -> Step:
        written = Step(JOB, len(self.steps) + 1, kind, json.loads(json.dumps(payload)), 1, NOW)
        self.steps.append(written)
        self.tokens_added += tokens
        if self.state is not None:
            self.state.apply(written)
        return written


def run_loop(
    transport: httpx2.AsyncBaseTransport,
    spec: dict[str, Any],
    steps: Sequence[Step] = (),
    *,
    workspace: Path | None = None,
    trigger: int | None = None,
    budget: int = 1_500_000,
) -> tuple[str, Writer, list[dict[str, Any]]]:
    """Run the worker loop to its end. Returns (outcome, step log, the tracker's issues)."""
    tracker = TestClient(create_tracker({"FAKE_TRACKER_FIRST_ISSUE": "100000"}), base_url=LOCAL)

    def forward(request: httpx2.Request) -> httpx2.Response:
        answer = tracker.request(
            request.method, str(request.url), content=request.content, headers=request.headers
        )
        return httpx2.Response(answer.status_code, content=answer.content)

    # The tools of a worker with a tracker and no sandbox, in its order.
    issues = create_issue_tool(FakeTracker(LOCAL), transport=httpx2.MockTransport(forward))
    root = workspace or Path("/nonexistent")
    settings = agent.AgentSettings(
        default_model=spec["model"],
        max_tokens=32_000,
        workspace_root=root.parent,
        summary_trigger=trigger,
    )
    writer = Writer(steps)

    async def go() -> str:
        async with httpx2.AsyncClient(transport=transport) as http:
            provider = AnthropicProvider(api_key="k", base_url=LOCAL, http_client=http)
            try:
                status, _ = await asyncio.wait_for(
                    agent._loop(
                        writer,  # type: ignore[arg-type]
                        provider,
                        ToolRegistry([READ_FILE, issues]),
                        job(spec, budget),
                        JobSpec.model_validate(spec),
                        settings,
                        root,
                        Lease(JOB, 1, timedelta(seconds=60), claimed_at=time.monotonic()),
                        list(steps),
                        asyncio.Event(),
                        Halt(JOB),
                    ),
                    timeout=120,
                )
                return status.value
            except agent._JobFailed as failed:
                return f"failed: {failed}"
            finally:
                await provider.aclose()

    outcome = asyncio.run(go())
    return outcome, writer, tracker.get("/issues").json()


def as_api(steps: Sequence[Step]) -> list[dict[str, Any]]:
    """Steps as `GET /jobs/{id}/steps` returns them."""
    return [
        {
            "seq": step.seq,
            "kind": step.kind.value,
            "payload": step.payload,
            "lease_epoch": step.lease_epoch,
            "created_at": step.created_at.isoformat(),
        }
        for step in steps
    ]


@dataclasses.dataclass(frozen=True)
class FakeRun:
    outcome: str
    steps: list[dict[str, Any]]
    issues: list[dict[str, Any]]
    review: Any


def stub_count(request: dict[str, Any]) -> int:
    """The same stub as for the fixture log: the bytes of the text plus 7."""
    return len(request["messages"][0]["content"].encode()) + 7


def fake_run(rules: ModuleType, workspace: Path, with_files: bool) -> FakeRun:
    """The scripted reviewer on the short job of the committed seed, at the fake's trigger."""
    review = rules.generate(read_seed(ROOT / rules.PUBLIC_SEED_FILE), SHORT)
    scenario = parse_scenario(json.dumps(rules.scripted_reviewer(review)))
    workspace.mkdir()
    if with_files:
        for name, content in review.files.items():
            (workspace / name).parent.mkdir(parents=True, exist_ok=True)
            (workspace / name).write_text(content)
    app = create_app({rules.FAKE_MODEL: scenario})
    spec = {"prompt": review.prompt, "system": review.system, "model": rules.FAKE_MODEL}
    outcome, writer, issues = run_loop(
        httpx2.ASGITransport(app=app),
        spec,
        workspace=workspace,
        trigger=rules.FAKE_TRIGGER,
        budget=rules.SHORT.budget_cap,
    )
    return FakeRun(outcome, as_api(writer.steps), issues, review)


@pytest.fixture(scope="module")
def run_with_files(rules: ModuleType, tmp_path_factory: pytest.TempPathFactory) -> FakeRun:
    return fake_run(rules, tmp_path_factory.mktemp("with") / "w", with_files=True)


@pytest.fixture(scope="module")
def run_without_files(rules: ModuleType, tmp_path_factory: pytest.TempPathFactory) -> FakeRun:
    return fake_run(rules, tmp_path_factory.mktemp("without") / "w", with_files=False)


# Fixed before the runs: between the count of a run that reads its files and
# the count of one whose reads are all errors.
RUN_FLOOR = 100_000


def grade_run(
    grader: ModuleType, rules: ModuleType, counting: ModuleType, run: FakeRun, shape: Any
) -> Any:
    counted = counting.crossing(
        run.steps, stub_count, model=rules.MODEL, chunk_bytes=rules.CHUNK_BYTES
    )
    job = rules.reduced({"status": run.outcome, "takeovers": 0, "error": None})
    given = grader.Log(
        job=job,
        steps=run.steps,
        issues=run.issues,
        questions=[],
        kills=[],
        failpoint_lines=0,
        verify=0,
        count=counted,
    )
    return grader.grade(given, grader.answers(run.review), shape=shape, crossing_floor=RUN_FLOOR)


def test_a_run_that_reads_its_files_is_over_the_floor_of_the_count(
    grader: ModuleType, rules: ModuleType, counting: ModuleType, run_with_files: FakeRun
) -> None:
    """Graded as a full job, so the count is part of the verdict."""
    found = grade_run(grader, rules, counting, run_with_files, rules.FULL)

    assert run_with_files.outcome == "succeeded"
    assert found.counts["crossing"]["total"] > RUN_FLOOR
    assert found.counts["crossing"]["partial"] is False
    assert "R3" not in names(found)


def test_a_run_whose_reads_are_all_errors_is_under_the_floor_of_the_count(
    grader: ModuleType, rules: ModuleType, counting: ModuleType, run_without_files: FakeRun
) -> None:
    found = grade_run(grader, rules, counting, run_without_files, rules.FULL)
    results = [s["payload"] for s in run_without_files.steps if s["kind"] == "tool_result"]
    errors = [payload for payload in results if payload["is_error"]]

    assert run_without_files.outcome == "succeeded"
    assert len(errors) == 37
    assert 0 < found.counts["crossing"]["total"] < RUN_FLOOR
    assert found.clauses.get("R3") == 1
    assert found.counts["files"]["read"] == 0


def test_the_scripted_reviewer_summarises_at_least_seven_times_at_the_fakes_trigger(
    rules: ModuleType, run_with_files: FakeRun
) -> None:
    summaries = [step for step in run_with_files.steps if step["kind"] == "summary"]
    requests = [step for step in run_with_files.steps if step["kind"] == "model_request"]

    assert rules.FAKE_TRIGGER == 20_000
    assert len(summaries) >= 7
    assert {step["payload"]["summary_trigger"] for step in requests} == {20_000}
    assert not [step for step in summaries if "problem" in step["payload"]]


def test_the_scripted_reviewer_meets_every_clause_but_the_kills_a_stack_makes(
    grader: ModuleType, rules: ModuleType, counting: ModuleType, run_with_files: FakeRun
) -> None:
    """The answers come from the generated job. No worker is killed in process."""
    found = grade_run(grader, rules, counting, run_with_files, rules.SHORT)
    key = answer_key(run_with_files.review)

    assert names(found) == ["kill_failpoint", "kill_2", "kill_3", "takeovers"]
    assert found.verdict == "INCONCLUSIVE"
    assert found.floors["made"] == len(key.made) == 12
    assert found.floors["cleared"] == len(key.cleared) == 4
    assert found.floors["windows"] >= 3
    assert found.floors["summaries"] >= 7
    assert found.counts["titles"] == {
        "found": 32,
        "missed": 0,
        "false": 0,
        "malformed": 0,
        "repeated": 0,
        "refused": 0,
    }
    assert found.counts["files"] == {
        "read": 36,
        "never_read": 0,
        "bytes": 36 * 16_384,
        "share": 1.0,
    }
    assert found.counts["recall"]["in_class"] <= 8
    assert found.counts["recall"]["rereads"] == 0


def test_the_answers_of_a_generated_job(grader: ModuleType, run_with_files: FakeRun) -> None:
    review = run_with_files.review
    key = answer_key(review)
    known = grader.answers(review)
    corpus = [content for name, content in review.files.items() if name != "index.txt"]
    carried = [planted for planted in review.planted if planted.kind == "carried"]

    assert (known.made, known.cleared, known.code) == (key.made, key.cleared, key.reference_code)
    assert known.violations == frozenset(key.key_full)
    assert len(known.grants) == 3_862
    assert known.corpus == {digest(content): 16_384 for content in corpus}
    assert set(known.files) == {*key.made, *key.cleared, *(p.grant.id for p in carried)}
    for planted in review.planted:
        if planted.grant.id in known.files:
            assert known.files[planted.grant.id] == digest(corpus[planted.row])
    assert known.carried == tuple((p.grant.id, digest(corpus[p.fact_row])) for p in carried)
    assert len(known.carried) == 8
