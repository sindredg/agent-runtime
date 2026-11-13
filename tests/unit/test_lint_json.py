"""The JSON lint script accepts valid files and names the broken ones."""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "lint_json.py"


def lint(*paths: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed command, paths come from the test
        [sys.executable, str(SCRIPT), *map(str, paths)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_valid_files_pass(tmp_path: Path) -> None:
    good = tmp_path / "good.json"
    good.write_text('{"a": [1, 2, {"b": null}]}')
    assert lint(good).returncode == 0


@pytest.mark.parametrize(
    "text", ['{"a": 1,}', "{'a': 1}", "", '{"a": 1, "a": 2}', '{"a": {"b": 1, "b": 1}}']
)
def test_a_broken_file_fails_and_is_named(tmp_path: Path, text: str) -> None:
    good, bad = tmp_path / "good.json", tmp_path / "bad.json"
    good.write_text("{}")
    bad.write_text(text)
    result = lint(good, bad)
    assert result.returncode == 1
    assert "bad.json" in result.stderr
    assert "good.json" not in result.stderr
