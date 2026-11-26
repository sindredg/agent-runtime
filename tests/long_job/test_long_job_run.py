"""One generated access review as a job on the long stack, graded by its outcome.

The first five tests are the job's own: each reads one count of the record
and has one assert with a fixed message, so a failure prints the count and
the message and nothing of the job. The last two hold the fake provider's
run to its pass line, and look for the job's answers in what the session
printed, recorded and kept.

Every test here is of mode `must-fail` and skips in another mode. The job
runs once, when the first test asks for its record.
"""

from typing import Any

import pytest

from .conftest import OUTPUT, Stack, markers_in, pass_line_misses

# The clauses of the runtime's own rules in the job's log, and the floors.
LOG_CLAUSES = ("R2", "R5", "R6", "crash")
FLOORS = ("floor_made", "floor_cleared", "floor_windows", "floor_summaries")


def test_every_counted_violation_of_a_notice_has_an_issue(record: dict[str, Any]) -> None:
    missed = record["clauses"].get("R7", 0)
    assert missed == 0, "a counted notice-violating grant has no issue"


def test_no_counted_grant_a_notice_clears_has_an_issue(record: dict[str, Any]) -> None:
    filed = record["clauses"].get("R8", 0)
    assert filed == 0, "a counted notice-cleared grant has an issue"


def test_every_issue_after_the_first_summary_holds_the_reference_code(
    record: dict[str, Any],
) -> None:
    without_code = record["clauses"].get("R9", 0)
    assert without_code == 0, "an issue after the first summary lacks the reference code"


def test_the_log_holds(record: dict[str, Any]) -> None:
    """verify finds the chain whole, the tools are the two, no operation has an issue too many,
    and no worker crashed on the job."""
    broken = sum(record["clauses"].get(name, 0) for name in LOG_CLAUSES)
    assert broken == 0, "the log, the tools or the issues break a rule, or a worker crashed"


def test_the_run_shows_enough(record: dict[str, Any]) -> None:
    """Every floor is met. A record with no floors measured shows nothing."""
    short = sum(record["clauses"].get(name, 0) for name in FLOORS) + int(not record["floors"])
    assert short == 0, "the run is under a floor of the pass line"


def test_fake_run_pass_line(record: dict[str, Any]) -> None:
    """On the fake provider: PASS, three kills landed, no question, at least six summaries."""
    if record["provider"] != "fake provider":
        pytest.skip("the pass line of the fake provider's run")
    missed = pass_line_misses(record)
    assert missed == [], "the fake provider's run misses its pass line"


def test_no_answer_is_in_the_record_the_output_or_the_saved_logs(
    record: dict[str, Any], stack: Stack
) -> None:
    """Neither the reference code, a decided grant id, a notice line nor a file's first line.

    The output is what pytest captured and reported for the tests before
    this one, the job's run among them. It has to hold the record's line:
    with capture off it would hold nothing to look in.
    """
    if record["provider"] != "fake provider":
        pytest.skip("the pass line of the fake provider's run")
    written = (stack.directory / "results.jsonl").read_text(encoding="utf-8")
    logs = [(stack.directory / "logs" / name).read_text(encoding="utf-8") for name in stack.logs]
    output = "\n".join(OUTPUT)
    empty = [
        name
        for name, held in (
            ("answers", stack.markers),
            ("output", "RESULT {" in output),
            ("logs", logs),
        )
        if not held
    ]
    assert empty == [], "nothing to look for, or nothing to look in"
    found = markers_in([written, output, *logs], stack.markers)
    assert found == 0, "the record, the output or a saved worker log holds an answer of the job"
