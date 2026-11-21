"""Guarded changes: what a pull request changed in the tests and in what runs them.

A guarantee is tested only while its test stays as strict as it was. A pull
request can change a mechanism and the test that guards it, so the pull
request has to say so in its own text (ADR 0024). This script works out the
section that text must carry, from two commits:

- under `tests/`: each removed test, the changed and the added tests per
  file, changed support code (fixtures, helpers, imports), and every other
  file that was added, changed or removed;
- entries of `scripts/switched_off.py` that were removed or changed, and a
  change to the rest of that script;
- whole files that decide what runs: the workflows, the make files, the
  pytest settings wherever they are, and this script.

Both commits are read as text, through git. Nothing of either is imported,
built or run, so the script can judge a pull request it does not trust.

    python -I scripts/guarded_changes.py BASE HEAD          print the section
    python -I scripts/guarded_changes.py BASE HEAD --check  and compare it with PR_BODY

BASE is the branch the pull request goes into. The comparison starts at the
commit the two have in common. `--check` reads the pull request's text from
the environment variable PR_BODY and exits 1 unless it holds the section
where its reader sees it. Exit 2: the two commits could not be compared.
"""

import ast
import copy
import json
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

HEADING = "## Guarded changes"
NOTHING = "No guarded changes."

TESTS = "tests/"
LIST = "scripts/switched_off.py"
ENTRIES = "ENTRIES"
# A file of this name, wherever it is: a workspace member's plugin loads too.
SETTINGS = "pyproject.toml"
# Whole files: a change, an addition and a removal are each listed. pytest and
# make each read some of these before the file the repository has today.
WHOLE_FILES = frozenset(
    {
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
    }
)
WHOLE_DIRECTORIES = (".github/workflows/",)

_HEADING_LINE = re.compile(r"^#{1,6}[ \t]")
# The start of a tag, a comment or another piece of HTML.
_HTML = re.compile(r"<[A-Za-z/!?]")
_TEST_NAME = ("test", "Test")


class Unreadable(Exception):
    """A file does not parse, or has not the shape the comparison needs."""


class Unjudged(Exception):
    """The two commits cannot be compared."""


def guarded(path: str) -> bool:
    return (
        path.startswith(TESTS)
        or path == LIST
        or Path(path).name == SETTINGS
        or path in WHOLE_FILES
        or path.startswith(WHOLE_DIRECTORIES)
    )


def _without_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    first = body[0] if body else None
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        return body[1:]
    return body


def _is_test(node: ast.stmt) -> bool:
    return isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test")


def _test_print(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """What a test is: every part of it but its docstring, so no part is left out by name."""
    bare = copy.copy(node)
    bare.body = _without_docstring(node.body)
    return ast.dump(bare)


def _names(target: ast.expr) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Tuple | ast.List):
        return [name for item in target.elts for name in _names(item)]
    if isinstance(target, ast.Starred):
        return _names(target.value)
    return []


def _bound_otherwise(node: ast.stmt) -> list[str]:
    """Names a statement assigns or deletes that pytest would read as a test or a test class.

    An import is not counted: it is support code, and a changed one is listed as that.
    """
    names: list[str] = []
    if isinstance(node, ast.Assign | ast.Delete):
        names = [name for target in node.targets for name in _names(target)]
    elif isinstance(node, ast.AnnAssign | ast.AugAssign):
        names = _names(node.target)
    return [name for name in names if name.startswith(_TEST_NAME)]


@dataclass(frozen=True)
class Module:
    # `name` or `Class::name` -> what the test is.
    tests: Mapping[str, str]
    # Everything else in the file.
    support: str


def _parsed(source: bytes) -> ast.Module:
    try:
        return ast.parse(source)
    # RecursionError, MemoryError: a file nested too deep to read.
    except (SyntaxError, ValueError, RecursionError, MemoryError) as error:
        raise Unreadable(type(error).__name__) from error


_BLOCKS = (ast.If, ast.Try, ast.TryStar, ast.With, ast.AsyncWith, ast.For, ast.AsyncFor, ast.While)


def _inner(block: ast.stmt) -> list[ast.stmt]:
    """The statements inside a block statement, every branch of it."""
    found: list[ast.stmt] = []
    for name in ("body", "orelse", "finalbody"):
        found += getattr(block, name, [])
    for handler in getattr(block, "handlers", []):
        found += handler.body
    return found


def read_module(source: bytes) -> Module:
    """A file's tests as pytest's defaults find them: `test*` in a module or a `Test*` class.

    A test inside a block of the module, an `if` for example, is found too.
    The block as a whole also counts as support code.

    Raises Unreadable when the file does not say plainly which tests it has:
    a test or a test class defined twice, or a name of that kind assigned
    or deleted. Which one runs is then for a person to read.
    """
    tree = _parsed(source)
    tests: dict[str, str] = {}
    classes: set[str] = set()
    support: list[str] = []

    def test(name: str, node: ast.stmt) -> None:
        assert isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)  # noqa: S101
        if name in tests:
            raise Unreadable(f"{name} is defined twice")
        tests[name] = _test_print(node)

    def take(nodes: list[ast.stmt], *, top: bool) -> None:
        for node in nodes:
            if rebound := _bound_otherwise(node):
                raise Unreadable(f"{rebound[0]} is not bound by a def or a class")
            if _is_test(node):
                test(node.name, node)  # type: ignore[attr-defined]
            elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                if node.name in classes:
                    raise Unreadable(f"{node.name} is defined twice")
                classes.add(node.name)
                rest = []
                for member in node.body:
                    if rebound := _bound_otherwise(member):
                        raise Unreadable(f"{rebound[0]} is not bound by a def")
                    if _is_test(member):
                        test(f"{node.name}::{member.name}", member)  # type: ignore[attr-defined]
                    else:
                        rest.append(member)
                if top:
                    # The class without its tests: its name, bases, keywords, marks.
                    shell = copy.copy(node)
                    shell.body = rest
                    support.append(ast.dump(shell))
            else:
                if top:
                    support.append(ast.dump(node))
                if isinstance(node, _BLOCKS):
                    take(_inner(node), top=False)

    try:
        take(tree.body, top=True)
    except RecursionError as error:
        raise Unreadable("RecursionError") from error
    return Module(tests, repr(support))


@dataclass(frozen=True)
class EntryList:
    # Name -> the entry as written.
    entries: Mapping[str, str]
    # Everything else in the script: the constants entries use, the judge.
    rest: str


def entry_list(source: bytes) -> EntryList:
    """The entries of the switched-off list: the literal dict assigned to `ENTRIES`."""
    tree = _parsed(source)
    entries: dict[str, str] | None = None
    rest: list[str] = []
    for node in tree.body:
        target: ast.expr | None = node.target if isinstance(node, ast.AnnAssign) else None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        if not (isinstance(target, ast.Name) and target.id == ENTRIES):
            rest.append(ast.dump(node))
            continue
        value = node.value if isinstance(node, ast.Assign | ast.AnnAssign) else None
        if entries is not None or not isinstance(value, ast.Dict):
            raise Unreadable(f"{ENTRIES} is not one literal dict")
        entries = {}
        for key, item in zip(value.keys, value.values, strict=True):
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                raise Unreadable(f"a key of {ENTRIES} is not a string")
            if key.value in entries:
                raise Unreadable(f"{ENTRIES} names {key.value!r} twice")
            entries[key.value] = ast.dump(item)
    if entries is None:
        raise Unreadable(f"no {ENTRIES}")
    return EntryList(entries, repr(rest))


def pytest_settings(source: bytes) -> str:
    """What a `pyproject.toml` tells pytest: its settings, and the plugins the project brings.

    As JSON, so `true` and `1` are not the same.
    """
    try:
        document = tomllib.loads(source.decode())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise Unreadable(type(error).__name__) from error
    tool, project = document.get("tool"), document.get("project")
    read = {
        "settings": tool.get("pytest") if isinstance(tool, dict) else None,
        "plugins": project.get("entry-points") if isinstance(project, dict) else None,
    }
    return json.dumps(read, sort_keys=True, default=str)


def _tests_lines(path: str, old: bytes | None, new: bytes | None) -> list[str]:
    if old is None:
        # Any new file: settings and a conftest act on older tests, a new
        # package can stand in for the code under test, and a new test file
        # runs in the same process as the others.
        return [f"- added file: {path}"]
    if not path.endswith(".py"):
        return [f"- removed file: {path}" if new is None else f"- changed file: {path}"]
    before = read_module(old)
    if new is None:
        removed = [f"- removed test: {path}::{name}" for name in before.tests]
        return removed or [f"- removed file: {path}"]
    after = read_module(new)
    lines = [f"- removed test: {path}::{name}" for name in before.tests if name not in after.tests]
    changed = sum(
        1 for name, was in before.tests.items() if name in after.tests and after.tests[name] != was
    )
    if changed:
        lines.append(f"- changed tests: {path}: {changed}")
    added = sum(1 for name in after.tests if name not in before.tests)
    if added:
        lines.append(f"- added tests: {path}: {added}")
    if before.support != after.support:
        lines.append(f"- changed support code: {path}")
    return lines


def _list_lines(old: bytes | None, new: bytes | None) -> list[str]:
    if old is None:
        return []
    if new is None:
        return [f"- removed file: {LIST}"]
    before, after = entry_list(old), entry_list(new)
    lines = []
    for name, was in before.entries.items():
        if name not in after.entries:
            lines.append(f"- removed entry: {name}")
        elif after.entries[name] != was:
            lines.append(f"- changed entry: {name}")
    if before.rest != after.rest:
        lines.append(f"- changed outside the entries: {LIST}")
    return lines


def _settings_lines(path: str, old: bytes | None, new: bytes | None) -> list[str]:
    nothing = pytest_settings(b"")
    before = nothing if old is None else pytest_settings(old)
    after = nothing if new is None else pytest_settings(new)
    return [] if before == after else [f"- changed pytest settings: {path}"]


def _whole_file_lines(path: str, old: bytes | None, new: bytes | None) -> list[str]:
    if old is None:
        return [f"- added file: {path}"]
    return [f"- removed file: {path}" if new is None else f"- changed file: {path}"]


def changes(base: Mapping[str, bytes], head: Mapping[str, bytes]) -> list[str]:
    """The lines of the section, sorted. `base` and `head` map guarded paths to their content."""
    lines: list[str] = []
    for path in sorted(base.keys() | head.keys()):
        old, new = base.get(path), head.get(path)
        if old == new or not guarded(path):
            continue
        try:
            if path.startswith(TESTS):
                lines += _tests_lines(path, old, new)
            elif path == LIST:
                lines += _list_lines(old, new)
            elif Path(path).name == SETTINGS:
                lines += _settings_lines(path, old, new)
            else:
                lines += _whole_file_lines(path, old, new)
        except Unreadable, RecursionError:
            # Not read, so not judged: the reader of the pull request looks.
            lines.append(f"- cannot be compared: {path}")
    return sorted(lines)


def section(lines: list[str]) -> str:
    return "\n".join([HEADING, "", *lines])


def problems(lines: list[str], body: str) -> list[str]:
    """What keeps the pull request's text from holding the section. Empty: it holds it.

    The section counts only where a reader of the pull request sees it, and
    the text is read here by rules that are simpler than Markdown's. So the
    heading and each line start at the left edge, the heading has an empty
    line on both sides, and no HTML comes before the section's end: a comment
    or a tag could hide it.
    """
    # Line ends as Markdown has them. Other separators stay inside their line.
    rows = [row.rstrip(" \t") for row in re.split(r"\r\n|\n|\r", body)]
    at = [index for index, row in enumerate(rows) if row == HEADING]
    if not at:
        return [f"the pull request's text has no {HEADING!r} section"] if lines else []
    if len(at) > 1:
        return [f"the pull request's text has {HEADING!r} more than once"]
    start = at[0]
    later = range(start + 1, len(rows))
    end = next((index for index in later if _HEADING_LINE.match(rows[index])), len(rows))
    said = {row for row in rows[start + 1 : end] if row}
    if not lines and not said:
        return []
    wrong = []
    if (start > 0 and rows[start - 1]) or (start + 1 < len(rows) and rows[start + 1]):
        wrong.append(f"{HEADING!r} needs an empty line before and after it")
    if any(_HTML.search(row) for row in rows[:end]):
        wrong.append(
            "the text has HTML or a `<...>` before the end of the section, which could hide it:"
            " put the section above it"
        )
    wanted = set(lines)
    return (
        wrong
        + [f"missing: {line}" for line in sorted(wanted - said)]
        + [f"not a guarded change of this pull request: {line}" for line in sorted(said - wanted)]
    )


def _git(repo: Path, *arguments: str) -> bytes:
    done = subprocess.run(  # noqa: S603 - a fixed program, arguments as a list
        ["git", "-C", str(repo), *arguments],  # noqa: S607 - git from the PATH
        check=True,
        capture_output=True,
    )
    return done.stdout


def _commit(repo: Path, name: str) -> str:
    # --end-of-options: a name is never read as an option of git.
    found = _git(repo, "rev-parse", "--verify", "--end-of-options", f"{name}^{{commit}}")
    return found.decode().strip()


LINK_MODE = "120000"
LINK = b"link to "
# Never printed as they are: they could start HTML in the pull request's text.
_MARKUP = "<>&"


def _shown(path: bytes) -> str:
    """A path as one printable line: other bytes and characters are written as escapes."""
    text = path.decode("utf-8", "backslashreplace")
    return "".join(
        f"\\x{ord(c):02x}"
        if c in _MARKUP
        else c
        if c.isprintable()
        else c.encode("unicode_escape").decode()
        for c in text
    )


def _blobs(repo: Path, commit: str) -> dict[str, tuple[str, str]]:
    """Guarded path -> (mode, blob id), for every file of the commit."""
    blobs = {}
    for record in _git(repo, "ls-tree", "-r", "-z", commit).split(b"\0"):
        if not record:
            continue
        about, name = record.split(b"\t", 1)
        mode, kind, blob = about.decode().split(" ")
        path = _shown(name)
        if kind == "blob" and guarded(path):
            blobs[path] = (mode, blob)
    return blobs


def read(repo: Path, base: str, head: str) -> tuple[dict[str, bytes], dict[str, bytes]]:
    """The guarded files that differ between the two commits' common ancestor and `head`."""
    head = _commit(repo, head)
    starts = _git(repo, "merge-base", "--all", _commit(repo, base), head).decode().split()
    if len(starts) != 1:
        raise Unjudged(f"the two commits have {len(starts)} common ancestors to start from, not 1")
    old, new = _blobs(repo, starts[0]), _blobs(repo, head)
    differing = [path for path in old.keys() | new.keys() if old.get(path) != new.get(path)]

    def content(blobs: dict[str, tuple[str, str]]) -> dict[str, bytes]:
        found = {}
        for path in differing:
            if path in blobs:
                mode, blob = blobs[path]
                held = _git(repo, "cat-file", "blob", blob)
                # A link holds a path, not the file: never the same as a file.
                found[path] = LINK + held if mode == LINK_MODE else held
        return found

    return content(old), content(new)


def main(arguments: list[str], repo: Path = Path()) -> int:
    check = "--check" in arguments
    commits = [argument for argument in arguments if argument != "--check"]
    if len(commits) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        base, head = read(repo, commits[0], commits[1])
    except subprocess.CalledProcessError as error:
        print(f"git failed: {error.stderr.decode(errors='replace').strip()}", file=sys.stderr)
        return 2
    except (OSError, Unjudged) as error:
        print(f"not compared: {error}", file=sys.stderr)
        return 2
    lines = changes(base, head)
    text = section(lines) if lines else NOTHING
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a") as out:
            out.write(text + "\n")
    if not check:
        return 0
    wrong = problems(lines, os.environ.get("PR_BODY", ""))
    if not wrong:
        return 0
    print("\nThe pull request's text must hold the section above, line for line:", file=sys.stderr)
    for problem in wrong:
        print(f"  {problem}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
