"""Guarded changes: what a pull request changed in the tests and in what runs them.

A guarantee is tested only while its test stays as strict as it was. A pull
request can change a mechanism and the test that guards it, so the pull
request has to say so in its own text (ADR 0024). This script works out the
section that text must carry, from two commits:

- test functions under `tests/` that were removed or changed, and changed
  support code there (fixtures, helpers, imports, other files);
- entries of `scripts/switched_off.py` that were removed or changed, and a
  change to the rest of that script;
- whole files that decide what runs: the workflows, the `Makefile`, the
  pytest settings, and this script.

A test that was only added is not listed: it guards more, not less.

Both commits are read as text, through git. Nothing of either is imported,
built or run, so the script can judge a pull request it does not trust.

    python -I scripts/guarded_changes.py BASE HEAD          print the section
    python -I scripts/guarded_changes.py BASE HEAD --check  and compare it with PR_BODY

BASE is the branch the pull request goes into. The comparison starts at the
commit the two have in common. `--check` reads the pull request's text from
the environment variable PR_BODY and exits 1 unless it holds the section.
"""

import ast
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
SETTINGS = "pyproject.toml"
# Whole files: a change, an addition and a removal are each listed.
# The other places pytest reads settings from come before `pyproject.toml`.
WHOLE_FILES = frozenset(
    {
        "scripts/guarded_changes.py",
        "Makefile",
        "conftest.py",
        "pytest.ini",
        ".pytest.ini",
        "tox.ini",
        "setup.cfg",
    }
)
WHOLE_DIRECTORIES = (".github/workflows/",)

_HEADING_LINE = re.compile(r"^#{1,6}\s")


class Unreadable(Exception):
    """A file does not parse, or has not the shape the comparison needs."""


def guarded(path: str) -> bool:
    return (
        path.startswith(TESTS)
        or path in (LIST, SETTINGS)
        or path in WHOLE_FILES
        or path.startswith(WHOLE_DIRECTORIES)
    )


def _dumped(nodes: list[ast.stmt] | list[ast.expr]) -> list[str]:
    return [ast.dump(node) for node in nodes]


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
    """What a test is, without its docstring: its marks, arguments and body."""
    return repr(
        (
            type(node).__name__,
            _dumped(node.decorator_list),
            ast.dump(node.args),
            _dumped(_without_docstring(node.body)),
        )
    )


@dataclass(frozen=True)
class Module:
    # `name` or `Class::name` -> what the test is.
    tests: Mapping[str, str]
    # Everything else in the file.
    support: str


def read_module(source: bytes) -> Module:
    """A file's tests as pytest's defaults find them: `test*` in a module or a `Test*` class."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as error:
        raise Unreadable(str(error)) from error
    tests: dict[str, str] = {}
    support: list[str] = []
    for node in tree.body:
        if _is_test(node):
            assert isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)  # noqa: S101
            tests[node.name] = _test_print(node)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            rest = []
            for member in node.body:
                if _is_test(member):
                    assert isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef)  # noqa: S101
                    tests[f"{node.name}::{member.name}"] = _test_print(member)
                else:
                    rest.append(member)
            head = (node.name, _dumped(node.bases), _dumped(node.decorator_list), _dumped(rest))
            support.append(repr(head))
        else:
            support.append(ast.dump(node))
    return Module(tests, repr(support))


@dataclass(frozen=True)
class EntryList:
    # Name -> the entry as written.
    entries: Mapping[str, str]
    # Everything else in the script: the constants entries use, the judge.
    rest: str


def entry_list(source: bytes) -> EntryList:
    """The entries of the switched-off list: the literal dict assigned to `ENTRIES`."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as error:
        raise Unreadable(str(error)) from error
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


def pytest_settings(source: bytes) -> object:
    """The `[tool.pytest]` table of a `pyproject.toml`, or None when it has none."""
    try:
        document = tomllib.loads(source.decode())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise Unreadable(str(error)) from error
    tool = document.get("tool")
    return tool.get("pytest") if isinstance(tool, dict) else None


def _tests_lines(path: str, old: bytes | None, new: bytes | None) -> list[str]:
    if not path.endswith(".py"):
        if old is None:
            return []
        return [f"- removed file: {path}" if new is None else f"- changed file: {path}"]
    if old is None:
        # A new conftest.py acts on tests that were there before it.
        return [f"- added file: {path}"] if Path(path).name == "conftest.py" else []
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


def _settings_lines(old: bytes | None, new: bytes | None) -> list[str]:
    before = None if old is None else pytest_settings(old)
    after = None if new is None else pytest_settings(new)
    return [] if before == after else [f"- changed pytest settings: {SETTINGS}"]


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
            elif path == SETTINGS:
                lines += _settings_lines(old, new)
            else:
                lines += _whole_file_lines(path, old, new)
        except Unreadable:
            # Not read, so not judged: the reader of the pull request looks.
            lines.append(f"- cannot be compared: {path}")
    return sorted(lines)


def section(lines: list[str]) -> str:
    return "\n".join([HEADING, "", *lines])


def stated(body: str) -> set[str] | None:
    """The lines under the section's heading in a pull request's text. None: no such heading."""
    found: set[str] | None = None
    inside = False
    for raw in body.splitlines():
        line = raw.strip()
        if line == HEADING:
            inside = True
            found = set() if found is None else found
        elif _HEADING_LINE.match(line):
            inside = False
        elif inside and line:
            assert found is not None  # noqa: S101 - set with `inside`
            found.add(line)
    return found


def problems(lines: list[str], body: str) -> list[str]:
    """What the pull request's text lacks or says beyond the section. Empty: it holds it."""
    said = stated(body)
    if said is None:
        return [f"the pull request's text has no {HEADING!r} section"] if lines else []
    wanted = set(lines)
    return [f"missing: {line}" for line in sorted(wanted - said)] + [
        f"not a guarded change of this pull request: {line}" for line in sorted(said - wanted)
    ]


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


def _blobs(repo: Path, commit: str) -> dict[str, str]:
    """Guarded path -> blob id, for every file of the commit. A link is listed as what it holds."""
    listed = _git(repo, "ls-tree", "-r", "-z", commit).decode("utf-8", "surrogateescape")
    blobs = {}
    for record in listed.split("\0"):
        if not record:
            continue
        about, path = record.split("\t", 1)
        _mode, kind, blob = about.split(" ")
        if kind == "blob" and guarded(path):
            blobs[path] = blob
    return blobs


def read(repo: Path, base: str, head: str) -> tuple[dict[str, bytes], dict[str, bytes]]:
    """The guarded files that differ between the two commits' common ancestor and `head`."""
    head = _commit(repo, head)
    start = _git(repo, "merge-base", _commit(repo, base), head).decode().strip()
    old, new = _blobs(repo, start), _blobs(repo, head)
    differing = [path for path in old.keys() | new.keys() if old.get(path) != new.get(path)]

    def content(blobs: dict[str, str]) -> dict[str, bytes]:
        return {
            path: _git(repo, "cat-file", "blob", blobs[path]) for path in differing if path in blobs
        }

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
    except OSError as error:
        print(f"git could not be run: {error}", file=sys.stderr)
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
