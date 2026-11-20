"""The switched-off list stays true to the code; its judge counts only the named failure.

A switched-off run proves a test can fail only if the edit still lands and
the test fails on the assertion that guards the mechanism: not on a setup
error, not on another assertion, and not by never running.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

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


def test_the_workflow_offers_exactly_the_listed_runs() -> None:
    workflow = (ROOT / ".github/workflows/manual-runs.yml").read_text()
    assert options(workflow, "switched_off") == list(switched_off.ENTRIES)
    assert "switched-off" in options(workflow, "suite")


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
        ('<failure message="AssertionError: assert 9 &lt;= 5">assert total &lt;= BUDGET</failure>',
         "ok"),
        ('<failure message="AssertionError">assert job.status == "failed"</failure>', "WRONG"),
        ('<error message="fixture failed">stack never came up</error>', "WRONG"),
        ('<skipped message="no stack" />', "WRONG"),
        ("", "WRONG"),
    ],
    ids=["named-failure", "other-assertion", "error", "skipped", "passed"],
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
