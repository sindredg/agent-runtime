"""The guarded-changes check names what a pull request changed in the tests (ADR 0024).

It reads two commits as text. A removed or changed test, a changed
switched-off entry and a change to what runs the tests each give a line, and
the pull request's text has to hold exactly those lines. The check runs on a
pull request it does not trust, so it must never import or run what it reads,
and its workflow must never check the pull request out.
"""

import importlib.util
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "guarded_changes.py"
WORKFLOW = (ROOT / ".github" / "workflows" / "guarded-changes.yml").read_text()


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("guarded_changes", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses look the module up while the class body runs.
    sys.modules["guarded_changes"] = module
    spec.loader.exec_module(module)
    return module


guarded = load()

A_TEST = "tests/unit/test_a.py"


def source(text: str) -> bytes:
    return textwrap.dedent(text).encode()


def lines(old: str | None, new: str | None, path: str = A_TEST) -> list[str]:
    """The section's lines for one file that went from `old` to `new`. None: not there."""
    base = {} if old is None else {path: source(old)}
    head = {} if new is None else {path: source(new)}
    found: list[str] = guarded.changes(base, head)
    return found


TWO_TESTS = """
    import os

    LIMIT = 3

    def helper():
        return LIMIT

    def test_one():
        assert helper() == 3

    def test_two():
        assert os.sep
"""


# --- tests ---


def test_a_removed_test_is_named() -> None:
    new = TWO_TESTS.replace("def test_two():\n        assert os.sep\n", "")
    assert lines(TWO_TESTS, new) == [f"- removed test: {A_TEST}::test_two"]


def test_a_renamed_test_is_a_removed_one() -> None:
    assert lines(TWO_TESTS, TWO_TESTS.replace("test_two", "test_2")) == [
        f"- removed test: {A_TEST}::test_two"
    ]


def test_changed_tests_are_counted_per_file() -> None:
    one = TWO_TESTS.replace("helper() == 3", "helper() >= 0")
    both = one.replace("assert os.sep", "assert True")
    assert lines(TWO_TESTS, one) == [f"- changed tests: {A_TEST}: 1"]
    assert lines(TWO_TESTS, both) == [f"- changed tests: {A_TEST}: 2"]


@pytest.mark.parametrize(
    "mark",
    ["@pytest.mark.skip", "@pytest.mark.xfail", '@pytest.mark.parametrize("n", [1])'],
)
def test_a_mark_on_a_test_changes_it(mark: str) -> None:
    new = TWO_TESTS.replace("def test_one():", f"{mark}\n    def test_one():")
    assert lines(TWO_TESTS, new) == [f"- changed tests: {A_TEST}: 1"]


def test_changed_arguments_change_a_test() -> None:
    new = TWO_TESTS.replace("def test_one():", "def test_one(tmp_path):")
    assert lines(TWO_TESTS, new) == [f"- changed tests: {A_TEST}: 1"]


def test_layout_comments_and_a_docstring_change_nothing() -> None:
    new = TWO_TESTS.replace(
        "def test_one():\n        assert helper() == 3",
        'def test_one():\n        """Says what it shows."""\n        # a comment\n\n'
        "        assert (helper()\n            == 3)",
    )
    assert new != TWO_TESTS
    assert lines(TWO_TESTS, new) == []


def test_an_added_test_is_not_listed() -> None:
    assert lines(TWO_TESTS, TWO_TESTS + "\n    def test_three():\n        assert LIMIT\n") == []
    assert lines(None, TWO_TESTS, "tests/unit/test_new.py") == []


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("LIMIT = 3", "LIMIT = 4"),
        ("import os", "import os\n    import sys"),
        ("return LIMIT", "return 3"),
        ("LIMIT = 3", "LIMIT = 3\n    pytestmark = pytest.mark.skip"),
    ],
)
def test_changed_support_code_names_the_file(old: str, new: str) -> None:
    assert lines(TWO_TESTS, TWO_TESTS.replace(old, new)) == [f"- changed support code: {A_TEST}"]


IN_A_CLASS = """
    class TestThing:
        size = 1

        def setup_method(self):
            self.made = True

        def test_made(self):
            assert self.made

        async def test_size(self):
            assert self.size == 1
"""


def test_tests_in_a_class_are_named_with_it() -> None:
    removed = IN_A_CLASS.replace("def test_made(self):\n            assert self.made\n", "")
    assert lines(IN_A_CLASS, removed) == [f"- removed test: {A_TEST}::TestThing::test_made"]
    changed = IN_A_CLASS.replace("self.size == 1", "self.size")
    assert lines(IN_A_CLASS, changed) == [f"- changed tests: {A_TEST}: 1"]


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("size = 1", "size = 2"),
        ("self.made = True", "self.made = False"),
        ("class TestThing:", "@pytest.mark.skip\n    class TestThing:"),
        ("class TestThing:", "class TestThing(Base):"),
    ],
)
def test_the_rest_of_a_test_class_is_support_code(old: str, new: str) -> None:
    assert lines(IN_A_CLASS, IN_A_CLASS.replace(old, new)) == [f"- changed support code: {A_TEST}"]


def test_a_removed_file_names_each_of_its_tests() -> None:
    assert lines(TWO_TESTS, None) == [
        f"- removed test: {A_TEST}::test_one",
        f"- removed test: {A_TEST}::test_two",
    ]
    assert lines("x = 1", None, "tests/unit/conftest.py") == [
        "- removed file: tests/unit/conftest.py"
    ]


def test_a_new_conftest_is_listed_because_it_acts_on_older_tests() -> None:
    assert lines(None, "x = 1", "tests/lab/conftest.py") == ["- added file: tests/lab/conftest.py"]
    assert lines("x = 1", "x = 2", "tests/lab/conftest.py") == [
        "- changed support code: tests/lab/conftest.py"
    ]


def test_another_kind_of_file_under_tests_is_listed_whole() -> None:
    path = "tests/integration/compose.orphans.yml"
    assert lines("a: 1", "a: 2", path) == [f"- changed file: {path}"]
    assert lines("a: 1", None, path) == [f"- removed file: {path}"]
    assert lines(None, "a: 1", path) == []


@pytest.mark.parametrize(("old", "new"), [(TWO_TESTS, "def test_one(:\n"), ("def (:\n", TWO_TESTS)])
def test_a_file_that_does_not_parse_is_named_and_not_judged(old: str, new: str) -> None:
    assert lines(old, new) == [f"- cannot be compared: {A_TEST}"]


IN_BLOCKS = """
    import sys

    if sys.platform == "linux":
        def test_here():
            assert 1
    else:
        def test_there():
            assert 2

    try:
        import fast
    except ImportError:
        class TestSlow:
            def test_it(self):
                assert 3
"""


def test_a_test_inside_a_block_of_the_module_is_named_too() -> None:
    gone = IN_BLOCKS.replace("def test_there():\n            assert 2", "pass")
    assert lines(IN_BLOCKS, gone) == [
        f"- changed support code: {A_TEST}",
        f"- removed test: {A_TEST}::test_there",
    ]
    assert lines(IN_BLOCKS, IN_BLOCKS.replace("assert 3", "assert 0")) == [
        f"- changed support code: {A_TEST}",
        f"- changed tests: {A_TEST}: 1",
    ]


def test_a_function_inside_a_test_is_not_a_test() -> None:
    old = "def test_one():\n    def test_inner():\n        pass\n    assert 1\n"
    assert lines(old, old.replace("def test_inner():\n        pass\n    ", "")) == [
        f"- changed tests: {A_TEST}: 1"
    ]


def test_a_file_nested_too_deep_to_read_is_named_and_not_judged() -> None:
    deep = "def test_one():\n    assert " + "(" * 5000 + "1" + ")" * 5000 + "\n"
    assert lines(TWO_TESTS, deep) == [f"- cannot be compared: {A_TEST}"]


# --- the switched-off list ---

THE_LIST = """
    TEST = "tests/a.py::test_x"

    ENTRIES: dict[str, SwitchedOff] = {
        "budget-off": SwitchedOff(issue=1, expect=(Expect(TEST, "spent <= BUDGET"),)),
        "resume-off": SwitchedOff(issue=2, expect=(Expect(TEST, "ran once"),)),
    }

    def judge():
        return 1
"""


def in_list(new: str | None) -> list[str]:
    return lines(THE_LIST, new, "scripts/switched_off.py")


def test_a_removed_and_a_changed_entry_are_named() -> None:
    removed = THE_LIST.replace(
        '"resume-off": SwitchedOff(issue=2, expect=(Expect(TEST, "ran once"),)),', ""
    )
    assert in_list(removed) == ["- removed entry: resume-off"]
    assert in_list(THE_LIST.replace('"ran once"', '"ran"')) == ["- changed entry: resume-off"]
    assert in_list(THE_LIST.replace('"spent <= BUDGET"', "None")) == ["- changed entry: budget-off"]


def test_an_added_entry_is_not_listed() -> None:
    added = THE_LIST.replace(
        '"resume-off"', '"cap-off": SwitchedOff(issue=3, expect=()),\n        "resume-off"'
    )
    assert added != THE_LIST
    assert in_list(added) == []


@pytest.mark.parametrize(
    ("old", "new"),
    [('"tests/a.py::test_x"', '"tests/a.py::test_y"'), ("return 1", "return 0")],
)
def test_a_change_outside_the_entries_is_named(old: str, new: str) -> None:
    # A constant an entry uses, and the judge.
    assert in_list(THE_LIST.replace(old, new)) == [
        "- changed outside the entries: scripts/switched_off.py"
    ]


@pytest.mark.parametrize(
    "new",
    [
        THE_LIST.replace("ENTRIES:", "OTHER:"),
        THE_LIST.replace("= {", "= dict(a=1) or {"),
        THE_LIST.replace('"resume-off"', '"budget-off"'),
        THE_LIST.replace('"resume-off"', "NAME"),
        THE_LIST + "\n    ENTRIES = {}\n",
    ],
)
def test_a_list_that_is_not_one_literal_dict_is_named_and_not_judged(new: str) -> None:
    assert in_list(new) == ["- cannot be compared: scripts/switched_off.py"]


def test_a_removed_list_is_named() -> None:
    assert in_list(None) == ["- removed file: scripts/switched_off.py"]


# --- what runs the tests ---

SETTINGS = """
    [project]
    dependencies = ["a==1"]

    [tool.pytest.ini_options]
    testpaths = ["tests/unit"]
    addopts = "--strict-markers"
"""


def test_only_the_pytest_settings_of_pyproject_are_guarded() -> None:
    def settings(new: str) -> list[str]:
        return lines(SETTINGS, new, "pyproject.toml")

    assert settings(SETTINGS.replace("a==1", "a==2")) == []
    assert settings(SETTINGS.replace("--strict-markers", "--deselect tests/unit")) == [
        "- changed pytest settings: pyproject.toml"
    ]
    assert settings(SETTINGS.replace("[tool.pytest.ini_options]", "[tool.other]")) == [
        "- changed pytest settings: pyproject.toml"
    ]
    assert settings("[tool") == ["- cannot be compared: pyproject.toml"]


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        ".github/workflows/guarded-changes.yml",
        "scripts/guarded_changes.py",
        "Makefile",
        "conftest.py",
        "pytest.ini",
        ".pytest.ini",
        "tox.ini",
        "setup.cfg",
    ],
)
def test_what_runs_the_tests_is_listed_whole(path: str) -> None:
    assert lines("a", "b", path) == [f"- changed file: {path}"]
    assert lines("a", None, path) == [f"- removed file: {path}"]
    # An addition counts here: a new settings file overrules the old one.
    assert lines(None, "a", path) == [f"- added file: {path}"]


@pytest.mark.parametrize("path", ["src/runtime/worker/agent.py", "docs/testing.md", "README.md"])
def test_other_files_are_not_guarded(path: str) -> None:
    assert lines("a = 1", "a = 2", path) == []


def test_the_lines_are_sorted() -> None:
    base = {A_TEST: source(TWO_TESTS), "Makefile": b"a", "scripts/switched_off.py": b"ENTRIES = {}"}
    head = {"Makefile": b"b"}
    assert guarded.changes(base, head) == [
        "- changed file: Makefile",
        "- removed file: scripts/switched_off.py",
        f"- removed test: {A_TEST}::test_one",
        f"- removed test: {A_TEST}::test_two",
    ]


# --- the pull request's text ---

LINES = [f"- changed tests: {A_TEST}: 1", f"- removed test: {A_TEST}::test_two"]


def test_the_section_as_printed_is_accepted() -> None:
    body = "## Summary\n\nWhat changed.\n\n" + guarded.section(LINES) + "\n\n## Evidence\n\n- ran\n"
    assert guarded.problems(LINES, body) == []


def test_line_ends_order_and_indentation_do_not_matter() -> None:
    body = f"## Guarded changes\r\n\r\n  {LINES[1]}\r\n{LINES[0]}  \r\n"
    assert guarded.problems(LINES, body) == []


def test_a_missing_line_and_a_line_too_many_are_each_named() -> None:
    body = f"## Guarded changes\n\n{LINES[0]}\n- removed test: tests/unit/test_b.py::test_x\n"
    assert guarded.problems(LINES, body) == [
        f"missing: {LINES[1]}",
        "not a guarded change of this pull request: - removed test: tests/unit/test_b.py::test_x",
    ]


def test_a_changed_count_does_not_pass_for_another() -> None:
    body = guarded.section([LINES[0].replace(": 1", ": 2"), LINES[1]])
    assert len(guarded.problems(LINES, body)) == 2


@pytest.mark.parametrize(
    "body",
    ["", "## Summary\n\n" + "\n".join(LINES), "### Guarded changes\n\n" + "\n".join(LINES)],
)
def test_lines_outside_the_section_do_not_count(body: str) -> None:
    assert guarded.problems(LINES, body) == [
        "the pull request's text has no '## Guarded changes' section"
    ]


@pytest.mark.parametrize(
    "body",
    [
        "<!--\n## Guarded changes\n\n{0}\n{1}\n-->",
        "<!--\n## Guarded changes\n\n{0}\n{1}\n## x -->",
        "<!-- a note\n\n## Guarded changes\n\n{0}\n{1}\n",
        "## Guarded changes\n\n{0}\n<!-- {1} -->\n",
        "## Guarded changes\n\n{0}\n<!--\n{1}\n-->\n",
    ],
)
def test_a_section_or_a_line_in_a_comment_does_not_count(body: str) -> None:
    # A comment is not shown to whoever reads the pull request.
    assert guarded.problems(LINES, body.format(*LINES)) != []


def test_a_comment_elsewhere_changes_nothing() -> None:
    body = "<!-- a template's hint -->\n" + guarded.section(LINES) + "\n<!-- more -->\n"
    assert guarded.problems(LINES, body) == []


def test_the_next_heading_ends_the_section() -> None:
    body = f"## Guarded changes\n\n{LINES[0]}\n\n## Evidence\n\n{LINES[1]}\n"
    assert guarded.problems(LINES, body) == [f"missing: {LINES[1]}"]


def test_nothing_changed_needs_no_section_and_allows_no_claim() -> None:
    assert guarded.problems([], "## Summary\n\nDocs only.") == []
    assert guarded.problems([], "## Guarded changes\n\n") == []
    assert guarded.problems([], guarded.section(LINES[:1])) == [
        f"not a guarded change of this pull request: {LINES[0]}"
    ]


# --- two commits of a repository ---


class Repo:
    """A git repository with a `main` branch, made for one test."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.git("init", "--quiet", "--initial-branch=main")

    def git(self, *arguments: str) -> str:
        done = subprocess.run(  # noqa: S603 - fixed program, arguments from the test
            [  # noqa: S607 - git from the PATH
                "git",
                "-c",
                "user.name=test",
                "-c",
                "user.email=test@example.invalid",
                "-c",
                "commit.gpgsign=false",
                *arguments,
            ],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        )
        return done.stdout.strip()

    def commit(self, files: dict[str, str | None]) -> str:
        """Write `files` (None removes one), commit everything, and return the commit."""
        for path, text in files.items():
            target = self.root / path
            if text is None:
                target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(textwrap.dedent(text))
        self.git("add", "--all")
        self.git("commit", "--quiet", "--allow-empty", "--message", "a commit")
        return self.git("rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Repo:
    # No setting of the machine reaches git, in the test or in the script.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.delenv("PR_BODY", raising=False)
    made = Repo(tmp_path)
    made.commit({A_TEST: TWO_TESTS, "tests/unit/test_b.py": "def test_b():\n    assert 1\n"})
    return made


def run(repo: Repo, *arguments: str) -> int:
    code: int = guarded.main(list(arguments), repo.root)
    return code


def test_only_what_the_branch_changed_is_listed(
    repo: Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    repo.git("switch", "--quiet", "--create", "work")
    repo.commit({A_TEST: TWO_TESTS.replace("helper() == 3", "True"), "src/a.py": "a = 1\n"})
    repo.git("switch", "--quiet", "main")
    # `main` moved on meanwhile. That is not the branch's change.
    repo.commit({"tests/unit/test_b.py": None, "Makefile": "all:\n"})

    assert run(repo, "main", "work") == 0
    assert capsys.readouterr().out == f"## Guarded changes\n\n- changed tests: {A_TEST}: 1\n"


def test_a_branch_without_guarded_changes_says_so(
    repo: Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    repo.git("switch", "--quiet", "--create", "work")
    repo.commit({"src/a.py": "a = 1\n"})
    assert run(repo, "main", "work", "--check") == 0
    assert capsys.readouterr().out == "No guarded changes.\n"


def test_the_check_is_red_until_the_text_holds_the_section(
    repo: Repo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo.git("switch", "--quiet", "--create", "work")
    repo.commit({A_TEST: None})
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    wanted = (
        f"## Guarded changes\n\n- removed test: {A_TEST}::test_one\n"
        f"- removed test: {A_TEST}::test_two\n"
    )

    # No text at all, as for a pull request opened without one.
    assert run(repo, "main", "work", "--check") == 1
    printed = capsys.readouterr()
    assert printed.out == wanted
    assert "has no '## Guarded changes' section" in printed.err
    assert summary.read_text() == wanted

    monkeypatch.setenv("PR_BODY", f"## Guarded changes\n\n- removed test: {A_TEST}::test_one\n")
    assert run(repo, "main", "work", "--check") == 1
    assert f"missing: - removed test: {A_TEST}::test_two" in capsys.readouterr().err

    monkeypatch.setenv("PR_BODY", "## Summary\n\nRemoves two tests.\n\n" + wanted)
    assert run(repo, "main", "work", "--check") == 0
    # Without --check the text is not read.
    monkeypatch.setenv("PR_BODY", "")
    assert run(repo, "main", "work") == 0


@pytest.mark.parametrize(
    "arguments",
    [
        ("main",),
        ("main", "work", "more"),
        ("main", "no-such-branch"),
        ("main", "--version"),
        ("--exec-path", "main"),
    ],
)
def test_a_start_that_cannot_be_judged_is_an_error_not_a_pass(
    repo: Repo, arguments: tuple[str, ...], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(repo, *arguments, "--check") == 2
    assert capsys.readouterr().out == ""


def test_nothing_of_the_pull_request_is_imported_or_run(
    repo: Repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ran = tmp_path / "ran"
    hostile = f"import pathlib\npathlib.Path({str(ran)!r}).touch()\nraise SystemExit(3)\n"
    repo.git("switch", "--quiet", "--create", "work")
    repo.commit(
        {
            A_TEST: hostile + "def test_one():\n    assert 1\n",
            "tests/unit/conftest.py": hostile,
            "conftest.py": hostile,
            "sitecustomize.py": hostile,
            "scripts/guarded_changes.py": hostile,
            "scripts/switched_off.py": hostile,
            "pyproject.toml": "[tool.pytest.ini_options]\naddopts = '-p no:terminal'\n",
            ".gitattributes": "* filter=run diff=run\n",
            ".github/workflows/ci.yml": "on: push\n",
        }
    )
    assert run(repo, "main", "work") == 0
    assert not ran.exists()
    assert capsys.readouterr().out.splitlines()[2:] == [
        "- added file: .github/workflows/ci.yml",
        "- added file: conftest.py",
        "- added file: scripts/guarded_changes.py",
        "- added file: tests/unit/conftest.py",
        "- changed pytest settings: pyproject.toml",
        f"- changed support code: {A_TEST}",
        f"- changed tests: {A_TEST}: 1",
        f"- removed test: {A_TEST}::test_two",
    ]


def test_a_link_is_read_as_what_it_holds_not_followed(
    repo: Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo.root / "tests" / "unit" / "test_link.py").symlink_to("/etc/hostname")
    repo.commit({})
    repo.git("switch", "--quiet", "--create", "work")
    (repo.root / "tests" / "unit" / "test_link.py").unlink()
    (repo.root / "tests" / "unit" / "test_link.py").symlink_to("/etc/passwd")
    repo.commit({})
    assert run(repo, "main", "work") == 0
    assert capsys.readouterr().out.splitlines()[2:] == [
        "- cannot be compared: tests/unit/test_link.py"
    ]


def test_a_file_that_became_a_link_is_a_changed_file(
    repo: Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    data = repo.root / "tests" / "unit" / "data.txt"
    data.write_text("other.txt")
    repo.commit({})
    repo.git("switch", "--quiet", "--create", "work")
    data.unlink()
    data.symlink_to("other.txt")
    repo.commit({})
    assert run(repo, "main", "work") == 0
    assert capsys.readouterr().out.splitlines()[2:] == ["- changed file: tests/unit/data.txt"]


def test_a_path_is_printed_as_one_line_whatever_bytes_it_holds(
    repo: Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    odd = os.fsencode(repo.root / "tests" / "unit") + b"/a\nb\xff.yml"
    Path(os.fsdecode(odd)).write_text("a: 1\n")
    repo.commit({})
    repo.git("switch", "--quiet", "--create", "work")
    Path(os.fsdecode(odd)).write_text("a: 2\n")
    repo.commit({})
    assert run(repo, "main", "work") == 0
    assert capsys.readouterr().out.splitlines()[2:] == ["- changed file: tests/unit/a\\nb\\xff.yml"]


def test_the_script_uses_only_the_standard_library() -> None:
    # It runs with `python -I` and no project: no dependency is installed.
    done = subprocess.run(  # noqa: S603 - fixed program and script
        [sys.executable, "-I", str(SCRIPT), "HEAD", "HEAD"],
        cwd=ROOT,
        # Not the job's own summary file, when this runs in CI.
        env={k: v for k, v in os.environ.items() if k not in ("GITHUB_STEP_SUMMARY", "PR_BODY")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert (done.returncode, done.stdout) == (0, "No guarded changes.\n"), done.stderr


# --- the workflow ---


def test_the_workflow_runs_mains_copy_on_every_pull_request_and_on_an_edit_of_its_text() -> None:
    trigger = "\non:\n  pull_request_target:\n    types: [opened, reopened, synchronize, edited]\n"
    assert trigger in WORKFLOW
    assert WORKFLOW.count("\n  pull_request") == 1
    assert "ref: ${{ github.event.repository.default_branch }}" in WORKFLOW


def test_the_workflow_holds_no_secret_and_cannot_write() -> None:
    assert "\npermissions:\n  contents: read\n\n" in WORKFLOW
    assert WORKFLOW.count("permissions:") == 1
    for word in ("secrets.", "environment:", "id-token", "write"):
        assert word not in WORKFLOW
    assert WORKFLOW.count("persist-credentials: false") == WORKFLOW.count("actions/checkout@") == 1
    assert "enable-cache: false" in WORKFLOW


def test_the_workflow_never_checks_the_pull_request_out() -> None:
    # Its commits are fetched under a name of their own and read with git.
    for word in ("head.sha", "head.ref", "head_ref", "merge_commit", "git checkout", "git switch"):
        assert word not in WORKFLOW
    fetch = 'git fetch --no-tags origin "+refs/pull/${NUMBER}/head:refs/remotes/pull/head"'
    assert fetch in WORKFLOW
    assert "python -I scripts/guarded_changes.py HEAD refs/remotes/pull/head --check" in WORKFLOW
    assert "uv run --no-project" in WORKFLOW
    for word in ("uv sync", "pip install", "make ", "pytest"):
        assert word not in WORKFLOW


def test_the_pull_requests_text_reaches_the_script_as_a_variable_never_as_shell() -> None:
    steps = WORKFLOW.split("    steps:\n", 1)[1]
    commands = [line for line in steps.splitlines() if not line.lstrip().startswith("#")]
    used = [line.strip() for line in commands if "${{" in line]
    assert used == [
        "ref: ${{ github.event.repository.default_branch }}",
        "NUMBER: ${{ github.event.pull_request.number }}",
        "PR_BODY: ${{ github.event.pull_request.body }}",
    ]
