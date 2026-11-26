"""Which CI jobs a pull request runs, and how the long job is split over runners.

A pull request that changes only files under `docs/` skips the jobs that
build or start the stack. Any doubt runs them: no path, a path outside
`docs/`, a moved file, a push, or a failed `changes` job. The long job's
tests are split over runners, each test on exactly one.
"""

import ast
import importlib.util
import re
import shutil
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / ".github" / "workflows" / "ci.yml"
LONG_TESTS = ROOT / "tests" / "long" / "test_long_job.py"
GATE = [
    "    needs: changes",
    "    if: ${{ !cancelled() && needs.changes.outputs.code != 'false' }}",
]
# The jobs that build or start the stack, skipped when only docs change.
GATED = {"stack", "integration", "escape", "chaos", "lab", "kills", "long", "long-job"}
# What always runs: the decision, and lint and unit tests of the repository's files.
ALWAYS = {"changes", "python", "yaml"}
# The names the ruleset requires, which must stay as they are.
REQUIRED = {
    "Python lint, types and tests",
    "YAML and workflow lint",
    "Images and compose stack",
    "Integration tests",
    "Escape tests",
    "Chaos tests (short run)",
}


@pytest.fixture(scope="module")
def ci_paths() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ci_paths", ROOT / "scripts" / "ci_paths.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def jobs() -> dict[str, list[str]]:
    """Each job of ci.yml by its id, as its lines."""
    lines = CI.read_text().splitlines()
    start = lines.index("jobs:")
    found: dict[str, list[str]] = {}
    current = ""
    for line in lines[start + 1 :]:
        if match := re.fullmatch(r"  ([a-z][\w-]*):", line):
            current = match[1]
            found[current] = []
        elif current:
            found[current].append(line)
    return found


def step_run(job: list[str], name: str) -> str:
    """The `run` text of the step named `name`, without its indentation."""
    at = job.index(f"      - name: {name}")
    run = next(i for i in range(at, len(job)) if job[i] == "        run: |")
    body = []
    for line in job[run + 1 :]:
        if line and not line.startswith("          "):
            break
        body.append(line.removeprefix("          "))
    return "\n".join(body).strip() + "\n"


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        (["docs/testing.md"], False),
        (["docs/testing.md", "docs/diagrams/a.svg", "docs/adr/0001-x.md"], False),
        ([], True),
        (["README.md"], True),
        (["docs/testing.md", "src/runtime/cli.py"], True),
        # A file moved into docs/ is listed by both paths with --no-renames.
        (["docs/x.py", "scripts/x.py"], True),
        # git quotes a name with an unusual character.
        (['"docs/a\\tb.md"'], True),
        (["docsx/a.md"], True),
        (["docs"], True),
        (["tests/unit/notes.md"], True),
        ([".github/workflows/ci.yml"], True),
    ],
)
def test_only_paths_under_docs_skip_the_stack_jobs(
    ci_paths: ModuleType, paths: list[str], expected: bool
) -> None:
    assert ci_paths.code(paths) is expected


@pytest.mark.parametrize(
    ("stdin", "printed"),
    [
        ("docs/a.md\ndocs/b.md\n", "code=false\n"),
        ("docs/a.md\n\nsrc/x.py\n", "code=true\n"),
        ("", "code=true\n"),
        ("\n\n", "code=true\n"),
    ],
)
def test_the_script_prints_the_output_line(stdin: str, printed: str) -> None:
    done = subprocess.run(  # noqa: S603 - the repository's own script
        ["python3", "-I", str(ROOT / "scripts" / "ci_paths.py")],  # noqa: S607
        input=stdin,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert done.stdout == printed


def git(repo: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603 - fixed arguments
        [  # noqa: S607
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        timeout=30,
    )


def run_step(repo: Path, event: str) -> str:
    """Run the `changes` step's own text in `repo` and return what it wrote for the job."""
    output = repo / "output.txt"
    output.write_text("")
    script = step_run(jobs()["changes"], "Paths outside the docs")
    subprocess.run(  # noqa: S603 - the workflow's own step
        ["bash", "-e", "-c", script],  # noqa: S607
        cwd=repo,
        env={"PATH": "/usr/bin:/bin", "EVENT": event, "GITHUB_OUTPUT": str(output)},
        check=True,
        capture_output=True,
        timeout=30,
    )
    return output.read_text()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A checkout with the script and one commit of a file outside docs/."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "docs").mkdir()
    shutil.copyfile(ROOT / "scripts" / "ci_paths.py", tmp_path / "scripts" / "ci_paths.py")
    (tmp_path / "docs" / "a.md").write_text("one\n")
    (tmp_path / "scripts" / "tool.py").write_text("print('a')\nprint('b')\nprint('c')\n")
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def test_a_docs_change_skips_and_a_moved_file_does_not(repo: Path) -> None:
    (repo / "docs" / "a.md").write_text("two\n")
    git(repo, "commit", "-q", "-am", "docs")
    assert run_step(repo, "pull_request") == "code=false\n"
    # Moved into docs/ unchanged: git's rename detection alone would list only docs/tool.py.
    git(repo, "mv", "scripts/tool.py", "docs/tool.py")
    git(repo, "commit", "-q", "-m", "move")
    assert run_step(repo, "pull_request") == "code=true\n"


def test_a_push_and_a_diff_that_fails_run_everything(repo: Path) -> None:
    # One commit: HEAD^1 does not exist, git fails, and the script reads nothing.
    assert run_step(repo, "pull_request") == "code=true\n"
    (repo / "docs" / "a.md").write_text("two\n")
    git(repo, "commit", "-q", "-am", "docs")
    assert run_step(repo, "push") == "code=true\n"


def test_the_stack_jobs_are_gated_and_skip_only_on_an_explicit_false() -> None:
    found = jobs()
    gated = {name for name, lines in found.items() if all(line in lines for line in GATE)}
    assert gated == GATED
    assert set(found) == GATED | ALWAYS
    # The jobs that always run have no condition and wait for no job.
    for name in ALWAYS:
        assert not any(line.startswith(("    needs:", "    if:")) for line in found[name]), name
    changes = "\n".join(found["changes"])
    assert "      code: ${{ steps.paths.outputs.code }}" in changes
    assert "        id: paths" in changes
    assert "fetch-depth: 2" in changes


def test_the_required_checks_keep_their_names() -> None:
    names = {
        line.removeprefix("    name: ")
        for lines in jobs().values()
        for line in lines
        if line.startswith("    name: ")
    }
    assert names >= REQUIRED


def long_split() -> list[list[str]]:
    """The test names of each runner of the long job, as its matrix lists them."""
    lines = jobs()["long"]
    parts: list[list[str]] = []
    for i, line in enumerate(lines):
        if line == "            tests: >-":
            names = []
            for name in lines[i + 1 :]:
                if not name.startswith("              "):
                    break
                names.append(name.strip())
            parts.append(names)
    return parts


def test_every_test_of_the_long_suite_runs_on_exactly_one_runner() -> None:
    tree = ast.parse(LONG_TESTS.read_text())
    tests = [
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    ]
    parts = long_split()
    listed = [name for part in parts for name in part]
    assert len(parts) == 3
    assert all(parts)
    assert sorted(listed) == sorted(tests)
    assert len(tests) == 8
    assert "          LONG_TESTS: ${{ matrix.tests }}" in jobs()["long"]


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ({}, "tests/long"),
        (
            {"LONG_TESTS": "test_a test_b"},
            "tests/long/test_long_job.py::test_a tests/long/test_long_job.py::test_b",
        ),
    ],
)
def test_make_long_runs_the_whole_suite_unless_given_names(
    given: dict[str, str], expected: str
) -> None:
    done = subprocess.run(
        ["make", "-n", "--no-print-directory", "long"],  # noqa: S607
        cwd=ROOT,
        env={"PATH": "/usr/bin:/bin", "DOCKER_GID": "0", **given},
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    pytest_lines = [line for line in done.stdout.splitlines() if "pytest" in line]
    assert pytest_lines == [f"uv run --frozen pytest -v -s {expected}"]
