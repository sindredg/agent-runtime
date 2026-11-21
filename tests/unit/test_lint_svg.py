"""Exercise the SVG check through the command that CI runs."""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "lint_svg.py"
SVG = '<svg xmlns="http://www.w3.org/2000/svg"/>'


@pytest.mark.parametrize(
    ("content", "expected"),
    [(SVG, 0), ("<svg>", 1), ("<html/>", 1), ("<!DOCTYPE svg>" + SVG, 1)],
)
def test_svg_lint(tmp_path: Path, content: str, expected: int) -> None:
    path = tmp_path / "figure.svg"
    path.write_text(content)
    result = subprocess.run(  # noqa: S603 - fixed command with a test fixture
        [sys.executable, str(SCRIPT), str(path)], capture_output=True, text=True, check=False
    )
    assert result.returncode == expected
    if expected:
        assert str(path) in result.stderr
