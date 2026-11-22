"""The docs lint script accepts a page that can be skimmed and names what breaks a limit."""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "lint_docs.py"

DIAGRAM = "```mermaid\nflowchart LR\n    A --> B\n```\n"
TABLE = "| A | B |\n|---|---|\n| One. Two. | x |\n"
GOOD = f"# Page\n\nA short paragraph.\n\n{DIAGRAM}\n## Part\n\n{TABLE}"
# 81 characters a line: five lines are more than four lines of 80.
LONG = "\n".join(["word " * 16 + "w"] * 5)


def lint(*paths: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed command, paths come from the test
        [sys.executable, str(SCRIPT), *map(str, paths)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def page(tmp_path: Path, text: str, name: str = "page.md") -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_a_page_inside_the_limits_passes(tmp_path: Path) -> None:
    result = lint(page(tmp_path, GOOD))
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("markup", "expected"),
    [
        ("![Job handoff](diagram.svg)", 0),
        ("[Job handoff](diagram.svg)", 1),
        ("```text\n![Job handoff](diagram.svg)\n```", 1),
        ("![Job handoff](missing.svg)", 1),
    ],
)
def test_svg_counts_when_displayed(tmp_path: Path, markup: str, expected: int) -> None:
    page(tmp_path, '<svg xmlns="http://www.w3.org/2000/svg"/>', "diagram.svg")
    result = lint(page(tmp_path, GOOD.replace(DIAGRAM, markup + "\n")))
    assert result.returncode == expected, result.stderr


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        (GOOD + "\nline\n" * 80, "lines, at most 150"),
        (GOOD + "\n" + LONG + "\n", "a paragraph of"),
        (GOOD + "\n- short\n- " + LONG.replace("\n", "\n  ") + "\n", "a paragraph of"),
        (GOOD + "\n| A |\n|---|\n| " + "word " * 40 + "|\n", "a table cell of"),
        (GOOD + "\n| A |\n|---|\n| One. Two. Three. |\n", "a table cell of"),
        (GOOD.replace(DIAGRAM, ""), "no Mermaid diagram"),
        (GOOD + "\nSee [that](missing.md).\n", "no such file"),
        (GOOD + "\nSee [that](other.md#not-there).\n", "no such heading"),
        (GOOD + "\nSee [that](#not-there).\n", "no such heading"),
        (GOOD + "\nOne thing — and another.\n", "an em dash"),
    ],
)
def test_a_page_past_a_limit_fails_and_says_why(tmp_path: Path, text: str, problem: str) -> None:
    page(tmp_path, "# Other\n\n## There\n", "other.md")
    good, bad = page(tmp_path, GOOD, "good.md"), page(tmp_path, text, "bad.md")
    result = lint(good, bad)
    assert result.returncode == 1
    assert "bad.md" in result.stderr
    assert problem in result.stderr
    assert "good.md" not in result.stderr


def test_what_a_reader_does_not_see_is_not_counted(tmp_path: Path) -> None:
    page(tmp_path, "# Other\n\n## A `code` heading, twice\n", "other.md")
    link = "[there](other.md#a-code-heading-twice)"
    text = (
        GOOD
        + "\nSee "
        + ",\n".join([link] * 12)
        + ".\n"
        + "\n```text\n"
        + ("a line in a code block. " * 20 + "\n") * 3
        + "```\n"
        + "\n| A |\n|---|\n| `a.b.c.d. e. f.` is one name. |\n"
    )
    result = lint(page(tmp_path, text))
    assert result.returncode == 0, result.stderr


def test_an_accepted_record_keeps_its_text_but_needs_its_diagram(tmp_path: Path) -> None:
    kept = page(tmp_path, GOOD + "\n" + LONG + "\n", "docs/adr/0004-a-record.md")
    assert lint(kept).returncode == 0
    bare = page(tmp_path, "# Record\n\n" + LONG + "\n", "docs/adr/0005-a-record.md")
    assert "no Mermaid diagram" in lint(bare).stderr
    choice = page(tmp_path, "# Record\n\n" + LONG + "\n", "docs/adr/0001-a-record.md")
    assert lint(choice).returncode == 0
    later = page(tmp_path, GOOD + "\n" + LONG + "\n", "docs/adr/0013-a-record.md")
    assert "a paragraph of" in lint(later).stderr


SHA = "5d7c43b" + "0" * 33


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("code.py#L3", 1),
        ("code.py#L3-L5", 1),
        ("https://github.com/owner/repo/blob/main/code.py#L3", 1),
        ("https://github.com/owner/repo/blob/5d7c43b/code.py#L3", 1),
        (f"https://github.com/owner/repo/blob/{SHA}/code.py#L3", 0),
        (f"https://github.com/owner/repo/blob/{SHA}/code.py#L3-L5", 0),
        ("code.py", 0),
    ],
)
def test_a_line_link_names_a_commit(tmp_path: Path, target: str, expected: int) -> None:
    # A line number on a moving file points at another line after the next change.
    page(tmp_path, "one\ntwo\nthree\n", "code.py")
    result = lint(page(tmp_path, GOOD + f"\nSee [code.py:3]({target}).\n"))
    assert result.returncode == expected, result.stderr
    if expected:
        assert "a line link moves with the code" in result.stderr
