"""The chaos tests' result file: every line says how its test ended.

The file is the record the published results are taken from. A line from a
test that failed, or from a run during which the clock was set, must not
read like a pass. The chaos fixtures are run here on three small tests, in a
pytest process of their own and without the stack.
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

TESTS = """
import os
import time
from pathlib import Path

import tests.chaos.conftest as chaos

chaos.CHAOS_DIR = Path(os.environ["RESULTS_DIR"])


def test_passes():
    chaos.record("passes", {"runs": 1, "outcome": "the job is resumed from the log"})


def test_fails_after_it_recorded():
    chaos.record("fails", {"runs": 1})
    raise AssertionError("the pass line was not met")


def test_the_clock_is_set_while_it_runs():
    chaos.record("clock set", {"runs": 1})
    real = time.time
    # From here on the wall clock is five seconds ahead of the monotonic one.
    time.time = lambda: real() + 5
"""


def run_chaos_fixtures(tmp_path: Path) -> tuple[str, dict[str, dict[str, Any]]]:
    """Run TESTS under the chaos conftest. Returns pytest's output and the results by test."""
    (tmp_path / "test_results_file.py").write_text(TESTS, encoding="utf-8")
    results = tmp_path / "results"
    finished = subprocess.run(  # noqa: S603 - fixed command
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(REPO_ROOT / "pyproject.toml"),
            "-p",
            "tests.chaos.conftest",
            "-p",
            "no:cacheprovider",
            "-q",
            str(tmp_path / "test_results_file.py"),
        ],
        cwd=REPO_ROOT,
        env={**os.environ, "RESULTS_DIR": str(results)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    lines = (results / "results.jsonl").read_text(encoding="utf-8").splitlines()
    return finished.stdout, {entry["test"]: entry for entry in map(json.loads, lines)}


def test_every_result_line_says_how_its_test_ended(tmp_path: Path) -> None:
    output, results = run_chaos_fixtures(tmp_path)
    assert "1 failed, 2 passed, 1 error" in output, output
    assert set(results) == {"passes", "fails", "clock set"}

    assert results["passes"]["verdict"] == "passed"
    assert "void" not in results["passes"]
    # What the test recorded itself is kept as it was.
    assert results["passes"]["outcome"] == "the job is resumed from the log"
    # The result of a failed test is kept, with its seed and counts, and marked.
    assert results["fails"]["verdict"] == "failed"
    # The test body passed, and the clock check that follows it made the run void.
    assert results["clock set"]["verdict"] == "void"
    assert "wall clock moved +5.0 s" in results["clock set"]["void"]


def test_a_result_may_not_use_the_fields_the_verdict_is_written_to(tmp_path: Path) -> None:
    """Otherwise the verdict would overwrite what the test recorded, without a word."""
    spec = importlib.util.spec_from_file_location(
        "chaos_conftest", REPO_ROOT / "tests" / "chaos" / "conftest.py"
    )
    assert spec is not None
    assert spec.loader is not None
    chaos = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(chaos)
    for taken in ("verdict", "void"):
        with pytest.raises(ValueError, match=taken):
            chaos.record("a test", {"runs": 1, taken: "something"})
