"""The guarded-changes check names what a pull request changed in the tests (ADR 0024).

It reads two commits as text. A removed, changed or added test, a changed
switched-off entry and a change to what runs the tests each give a line, and
the pull request's text has to hold exactly those lines where a reader sees
them. The check runs on a pull request it does not trust, so it must never
import or run what it reads, and its workflow must never check the pull
request out.
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


def test_a_renamed_test_is_a_removed_and_an_added_one() -> None:
    assert lines(TWO_TESTS, TWO_TESTS.replace("test_two", "test_2")) == [
        f"- added tests: {A_TEST}: 1",
        f"- removed test: {A_TEST}::test_two",
    ]


def test_changed_tests_are_counted_per_file() -> None:
    one = TWO_TESTS.replace("helper() == 3", "helper() >= 0")
    both = one.replace("assert os.sep", "assert True")
    assert lines(TWO_TESTS, one) == [f"- changed tests: {A_TEST}: 1"]
    assert lines(TWO_TESTS, both) == [f"- changed tests: {A_TEST}: 2"]


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("def test_one():", "@pytest.mark.skip\n    def test_one():"),
        ("def test_one():", "@pytest.mark.xfail\n    def test_one():"),
        ("def test_one():", '@pytest.mark.parametrize("n", [1])\n    def test_one():'),
        ("def test_one():", "def test_one(tmp_path):"),
        ("def test_one():", "async def test_one():"),
        # An annotation and a type parameter are evaluated when the file loads.
        ("def test_one():", 'def test_one() -> pytest.skip("x"):'),
        ("def test_one():", 'def test_one[T = pytest.skip("x")]():'),
    ],
)
def test_every_part_of_a_test_but_its_docstring_is_the_test(old: str, new: str) -> None:
    assert lines(TWO_TESTS, TWO_TESTS.replace(old, new)) == [f"- changed tests: {A_TEST}: 1"]


def test_layout_comments_and_a_docstring_change_nothing() -> None:
    new = TWO_TESTS.replace(
        "def test_one():\n        assert helper() == 3",
        'def test_one():\n        """Says what it shows."""\n        # a comment\n\n'
        "        assert (helper()\n            == 3)",
    )
    assert new != TWO_TESTS
    assert lines(TWO_TESTS, new) == []


def test_added_tests_are_counted_because_they_run_beside_the_others() -> None:
    more = TWO_TESTS + "\n    def test_three():\n        assert LIMIT\n"
    assert lines(TWO_TESTS, more) == [f"- added tests: {A_TEST}: 1"]


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
        # A keyword of the class is evaluated when the file loads.
        ("class TestThing:", 'class TestThing(metaclass=pytest.skip("x")):'),
        ("class TestThing:", 'class TestThing[T = pytest.skip("x")]:'),
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


@pytest.mark.parametrize(
    "path",
    [
        # Each acts on tests that were there before it, or runs beside them.
        "tests/lab/conftest.py",
        "tests/unit/test_new.py",
        "tests/unit/pytest.ini",
        "tests/pyproject.toml",
        "tests/unit/runtime/__init__.py",
        "tests/integration/compose.orphans.yml",
    ],
)
def test_every_file_added_under_tests_is_named(path: str) -> None:
    assert lines(None, "x = 1", path) == [f"- added file: {path}"]


def test_another_kind_of_file_under_tests_is_listed_whole() -> None:
    path = "tests/integration/compose.orphans.yml"
    assert lines("a: 1", "a: 2", path) == [f"- changed file: {path}"]
    assert lines("a: 1", None, path) == [f"- removed file: {path}"]
    assert lines("x = 1", "x = 2", "tests/lab/conftest.py") == [
        "- changed support code: tests/lab/conftest.py"
    ]


@pytest.mark.parametrize(("old", "new"), [(TWO_TESTS, "def test_one(:\n"), ("def (:\n", TWO_TESTS)])
def test_a_file_that_does_not_parse_is_named_and_not_judged(old: str, new: str) -> None:
    assert lines(old, new) == [f"- cannot be compared: {A_TEST}"]


def test_a_file_nested_too_deep_to_read_is_named_and_not_judged() -> None:
    deep = "def test_one():\n    assert " + "(" * 5000 + "1" + ")" * 5000 + "\n"
    assert lines(TWO_TESTS, deep) == [f"- cannot be compared: {A_TEST}"]


@pytest.mark.parametrize(
    "more",
    [
        "del test_one",
        "test_one = None",
        "test_one: object = helper",
        "test_one, other = helper, helper",
        "def test_one():\n        pass",
        "if os.sep:\n        def test_one():\n            pass",
        "TestThing = None",
        "class TestThing:\n        pass\n\n    class TestThing:\n        pass",
        "class TestThing:\n        def test_x(self): pass\n        def test_x(self): pass",
        "class TestThing:\n        def test_x(self): pass\n        test_x = None",
    ],
)
def test_a_file_that_does_not_say_plainly_which_tests_it_has_is_not_judged(more: str) -> None:
    # Which of two definitions runs, or whether one still exists, is not worked out here.
    new = TWO_TESTS + "\n    " + more + "\n"
    assert lines(TWO_TESTS, new) == [f"- cannot be compared: {A_TEST}"]


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


def test_an_entrys_name_is_printed_as_one_line_without_markup() -> None:
    odd = THE_LIST.replace('"resume-off"', '"a<!--\\n- removed entry: budget-off"')
    assert lines(odd, THE_LIST, "scripts/switched_off.py") == [
        "- removed entry: a\\x3c!--\\n- removed entry: budget-off"
    ]


def test_an_added_entry_is_not_listed() -> None:
    added = THE_LIST.replace(
        '"resume-off"', '"cap-off": SwitchedOff(issue=3, expect=()),\n        "resume-off"'
    )
    assert added != THE_LIST
    assert in_list(added) == []


@pytest.mark.parametrize(
    ("old", "new"),
    [
        # A constant an entry uses, the judge, and a change of the list after it is made.
        ('"tests/a.py::test_x"', '"tests/a.py::test_y"'),
        ("return 1", "return 0"),
        ("def judge():", 'del ENTRIES["budget-off"]\n\n    def judge():'),
    ],
)
def test_a_change_outside_the_entries_is_named(old: str, new: str) -> None:
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

    [project.entry-points.console]
    run = "a:main"

    [tool.pytest]
    strict = true

    [tool.pytest.ini_options]
    testpaths = ["tests/unit"]
    addopts = "--strict-markers"
"""


@pytest.mark.parametrize("path", ["pyproject.toml", "services/fake_provider/pyproject.toml"])
def test_of_a_pyproject_the_pytest_settings_and_the_plugins_are_guarded(path: str) -> None:
    def settings(old: str, new: str) -> list[str]:
        return lines(SETTINGS, SETTINGS.replace(old, new), path)

    changed = [f"- changed pytest settings: {path}"]
    plugin = 'run = "a:main"\n[project.entry-points.pytest11]\nx = "p"'
    assert settings("a==1", "a==2") == []
    assert settings("--strict-markers", "--deselect tests/unit") == changed
    assert settings("[tool.pytest.ini_options]", "[tool.other]") == changed
    # Outside `ini_options`, and a value of another type that reads the same in Python.
    assert settings("strict = true", "strict = false") == changed
    assert settings("strict = true", "strict = 1") == changed
    # A plugin the project itself brings is loaded by pytest without being named.
    assert settings('run = "a:main"', plugin) == changed
    assert lines(SETTINGS, "[tool", path) == [f"- cannot be compared: {path}"]
    assert lines(None, SETTINGS, path) == changed
    assert lines(SETTINGS, None, path) == changed
    assert lines(None, '[project]\nname = "x"\n', path) == []


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        ".github/workflows/guarded-changes.yml",
        "scripts/guarded_changes.py",
        "Makefile",
        "makefile",
        "GNUmakefile",
        "conftest.py",
        "pytest.ini",
        ".pytest.ini",
        "pytest.toml",
        ".pytest.toml",
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
NO_SECTION = ["the pull request's text has no '## Guarded changes' section"]


def held(body: str, wanted: list[str] = LINES) -> list[str]:
    found: list[str] = guarded.problems(wanted, body)
    return found


def test_the_section_as_printed_is_accepted() -> None:
    body = "## Summary\n\nWhat changed.\n\n" + guarded.section(LINES) + "\n\n## Evidence\n\n- ran\n"
    assert held(body) == []
    assert held(guarded.section(LINES)) == []


def test_line_ends_order_and_spaces_at_a_lines_end_do_not_matter() -> None:
    assert held(f"## Guarded changes  \r\n\r\n{LINES[1]}\r\n{LINES[0]}\t\r\n") == []
    assert held(f"## Guarded changes\r\r{LINES[1]}\r{LINES[0]}") == []


def test_a_missing_line_and_a_line_too_many_are_each_named() -> None:
    body = f"## Guarded changes\n\n{LINES[0]}\n- removed test: tests/unit/test_b.py::test_x\n"
    assert held(body) == [
        f"missing: {LINES[1]}",
        "not a guarded change of this pull request: - removed test: tests/unit/test_b.py::test_x",
    ]


@pytest.mark.parametrize(
    "said",
    [
        LINES[0].replace(": 1", ": 2"),
        LINES[0] + "0",
        LINES[0][:-3],
        LINES[0].upper(),
        "  " + LINES[0],
        "> " + LINES[0],
        LINES[0] + "\u200b",
    ],
)
def test_a_line_counts_only_as_it_was_printed(said: str) -> None:
    assert held(guarded.section([said, LINES[1]])) == [
        f"missing: {LINES[0]}",
        f"not a guarded change of this pull request: {said}",
    ]


@pytest.mark.parametrize(
    "heading",
    [
        "### Guarded changes",
        "## Guarded changes and more",
        "## guarded changes",
        "  ## Guarded changes",
        "> ## Guarded changes",
        "## Summary",
    ],
)
def test_only_the_heading_as_printed_at_the_left_edge_starts_the_section(heading: str) -> None:
    assert held(heading + "\n\n" + "\n".join(LINES)) == NO_SECTION
    assert held("") == NO_SECTION


def test_the_next_heading_ends_the_section() -> None:
    body = f"## Guarded changes\n\n{LINES[0]}\n\n## Evidence\n\n{LINES[1]}\n"
    assert held(body) == [f"missing: {LINES[1]}"]


def test_the_section_is_there_once() -> None:
    body = guarded.section(LINES) + "\n\n" + guarded.section(LINES)
    assert held(body) == ["the pull request's text has '## Guarded changes' more than once"]


HIDDEN = "the text has HTML or a `<...>` before the end of the section"


@pytest.mark.parametrize(
    "body",
    [
        "<!--\n\n## Guarded changes\n\n{0}\n{1}\n-->",
        "<!-- a note\n\n## Guarded changes\n\n{0}\n{1}\n",
        "## Guarded changes\n\n{0}\n<!-- {1} -->\n",
        "## Guarded changes\n\n{0}\n<!--\n{1}\n-->\n",
        "<details>\n\n## Guarded changes\n\n{0}\n{1}\n</details>",
        "<?\n\n## Guarded changes\n\n{0}\n{1}\n?>",
        "<!X\n\n## Guarded changes\n\n{0}\n{1}\n>",
        "<![CDATA[\n\n## Guarded changes\n\n{0}\n{1}\n]]>",
        "A <b>word</b> in the summary.\n\n## Guarded changes\n\n{0}\n{1}\n",
    ],
)
def test_html_before_the_end_of_the_section_could_hide_it_and_is_refused(body: str) -> None:
    # A comment, a closed block or a tag: what a reader sees is not worked out here.
    assert any(problem.startswith(HIDDEN) for problem in held(body.format(*LINES)))


def test_html_after_the_section_changes_nothing() -> None:
    body = guarded.section(LINES) + "\n\n## Evidence\n\n<details>logs</details>\n<!-- more -->\n"
    assert held(body) == []
    # A text without the section, for a pull request without guarded changes.
    assert held("<!-- a template's hint -->\n## Summary\n\nDocs only.", []) == []


@pytest.mark.parametrize(
    "body",
    [
        # The title of a link definition runs over lines, and none of it is shown.
        '[x]: http://e.example "t\n## Guarded changes\n{0}\n{1}\n"',
        '[x]: http://e.example "t\n## Guarded changes\n\n{0}\n{1}\n"',
        "Some text\n## Guarded changes\n\n{0}\n{1}\n",
        "## Guarded changes\n{0}\n{1}\n",
    ],
)
def test_the_heading_needs_an_empty_line_on_both_sides(body: str) -> None:
    assert held(body.format(*LINES))[0] == (
        "'## Guarded changes' needs an empty line before and after it"
    )


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\x85", "\x0c", "\x0b", "\x1c"])
def test_only_a_line_end_of_markdown_ends_a_line(separator: str) -> None:
    # Python's own line splitting would see a section in the title of this link definition.
    hidden = separator.join(['[x]: http://e.example "t', "## Guarded changes", "", *LINES, '"'])
    assert held(hidden) == NO_SECTION


def test_nothing_changed_needs_no_section_and_allows_no_claim() -> None:
    assert held("## Summary\n\nDocs only.", []) == []
    assert held("## Guarded changes\n\n", []) == []
    assert held(guarded.section(LINES[:1]), []) == [
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


def printed(capsys: pytest.CaptureFixture[str]) -> list[str]:
    """The lines of the section the script printed, without its heading."""
    out = capsys.readouterr().out.splitlines()
    assert out[:2] == ["## Guarded changes", ""]
    return out[2:]


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
    shown = capsys.readouterr()
    assert shown.out == wanted
    assert "has no '## Guarded changes' section" in shown.err
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


def test_two_commits_that_meet_in_more_than_one_place_are_not_compared(
    repo: Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    repo.git("switch", "--quiet", "--create", "work")
    ours = repo.commit({A_TEST: None})
    repo.git("switch", "--quiet", "main")
    theirs = repo.commit({"src/a.py": "a = 1\n"})
    # Each takes the other in: two common ancestors, and which one to start from is open.
    repo.git("merge", "--quiet", "--no-edit", ours)
    repo.git("switch", "--quiet", "work")
    repo.git("merge", "--quiet", "--no-edit", theirs)

    assert run(repo, "main", "work", "--check") == 2
    shown = capsys.readouterr()
    assert (shown.out, "2 common ancestors" in shown.err) == ("", True)


def test_histories_that_never_meet_are_not_compared(
    repo: Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    repo.git("switch", "--quiet", "--orphan", "work")
    repo.commit({"src/a.py": "a = 1\n"})
    assert run(repo, "main", "work", "--check") == 2
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
            ".github/workflows/ci.yml": "on: push\n",
        }
    )
    assert run(repo, "main", "work") == 0
    assert not ran.exists()
    assert printed(capsys) == [
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
    link = repo.root / "tests" / "unit" / "test_link.py"
    link.symlink_to("/etc/hostname")
    repo.commit({})
    repo.git("switch", "--quiet", "--create", "work")
    link.unlink()
    link.symlink_to("/etc/passwd")
    (repo.root / "tests" / "unit" / "more").symlink_to("../../docs")
    repo.commit({})
    assert run(repo, "main", "work") == 0
    # A new link is a new file, whatever it leads to: pytest would follow it.
    assert printed(capsys) == [
        "- added file: tests/unit/more",
        "- cannot be compared: tests/unit/test_link.py",
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
    assert printed(capsys) == ["- changed file: tests/unit/data.txt"]


def test_a_path_is_printed_as_one_line_without_markup_whatever_bytes_it_holds(
    repo: Repo, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    odd = os.fsdecode(os.fsencode(repo.root / "tests" / "unit") + b"/a\nb\xff<!--c&.yml")
    Path(odd).write_text("a: 1\n")
    repo.commit({})
    repo.git("switch", "--quiet", "--create", "work")
    Path(odd).write_text("a: 2\n")
    repo.commit({})
    assert run(repo, "main", "work") == 0
    line = "- changed file: tests/unit/a\\nb\\xff\\x3c!--c\\x26.yml"
    assert printed(capsys) == [line]
    # The line can be stated: it does not read as HTML in the text.
    monkeypatch.setenv("PR_BODY", guarded.section([line]))
    assert run(repo, "main", "work", "--check") == 0


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

# Every line of the workflow that is not a comment. The job is safe because of
# what it does not do, and a list of forbidden words would miss the next way
# to run a pull request's code. So the whole of it is pinned: a change to the
# workflow changes this list, and both show in the pull request that makes it.
#
# - `pull_request_target`: main's copy of the workflow and the script run.
#   `edited`: a change of the pull request's text starts a run.
# - `contents: read`, no secret, no environment, no cache, no credentials kept.
# - The checkout is the default branch. The pull request's commits are fetched
#   under a name of their own and never checked out; the commit judged is the
#   one the run is for.
# - `--no-project` and `-I`: nothing is installed or importable from the checkout.
# - The pull request's number, commit and text reach a step as variables only.
THE_WORKFLOW = """\
name: Guarded changes
on:
  pull_request_target:
    types: [opened, reopened, synchronize, edited]
permissions:
  contents: read
concurrency:
  group: guarded-changes-${{ github.event.pull_request.number }}
  cancel-in-progress: true
jobs:
  guarded:
    name: Guarded changes
    runs-on: ubuntu-26.04
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1  # v7.0.1
        with:
          ref: ${{ github.event.repository.default_branch }}
          fetch-depth: 0
          persist-credentials: false
      - uses: astral-sh/setup-uv@68559e99cc807c1eb7e9238fd43a15bba460eec2  # v10.3.3
        with:
          version: "0.13.8"
          enable-cache: false
      - name: Fetch the pull request's commits
        env:
          NUMBER: ${{ github.event.pull_request.number }}
          COMMIT: ${{ github.event.pull_request.head.sha }}
        run: |
          git -c fetch.fsckObjects=true fetch --no-tags --no-recurse-submodules origin \\
            "+refs/pull/${NUMBER}/head:refs/remotes/pull/head"
          test "$(git rev-parse refs/remotes/pull/head)" = "$COMMIT"
      - name: Compare with the pull request's text
        env:
          PR_BODY: ${{ github.event.pull_request.body }}
        run: >-
          uv run --no-project --python "$(cat .python-version)"
          python -I scripts/guarded_changes.py HEAD refs/remotes/pull/head --check
"""


def test_the_workflow_is_exactly_the_reviewed_one() -> None:
    kept = [
        line for line in WORKFLOW.splitlines() if line.strip() and not line.lstrip().startswith("#")
    ]
    assert kept == THE_WORKFLOW.splitlines()
