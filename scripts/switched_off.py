"""Switched-off runs: remove one mechanism, run the tests that guard it, expect them to fail.

A guarantee is "tested" only with a run that removes its mechanism and fails
its test (README). Each entry below names the edit, the tests, and how each
test must end: failed on a named assertion, or passed for a stated reason.
The list is fixed before a run. A test that ends otherwise fails the run, and
is a finding for the results page, not a reason to change the entry.

The Manual runs workflow runs one entry at a time, on `main`, on the fake
provider (ADR 0021).

    python scripts/switched_off.py names     the entries, one per line
    python scripts/switched_off.py check     every edit matches once and still compiles
    python scripts/switched_off.py run NAME  edit the checkout, start the stack, run, judge

`run` changes files in the checkout. Run it on a clean checkout and restore
it with `git checkout -- .` afterwards.
"""

import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parents[1]
JUNIT = Path(".switched-off/junit.xml")
AGENT = "src/runtime/worker/agent.py"
KILLS = "tests/chaos/test_kills.py"
FAILPOINT = f"{KILLS}::test_a_job_survives_its_worker_stopping_at_the_failpoint"
HALT = "tests/integration/test_halt_store.py"


@dataclass(frozen=True)
class Edit:
    path: str
    # Must occur exactly once in the file.
    old: str
    new: str


@dataclass(frozen=True)
class Expect:
    node: str
    # Text the failure must carry: the assertion that guards the mechanism.
    # None: the test is expected to pass, for the reason in `passes_because`.
    fails_with: str | None
    passes_because: str = ""


@dataclass(frozen=True)
class SwitchedOff:
    issue: int
    what: str
    edits: tuple[Edit, ...]
    # `up`: the normal stack. `chaos`: three workers and a short lease.
    stack: Literal["up", "chaos"]
    expect: tuple[Expect, ...]


NOTHING_TO_REPLAY = (
    "the kill comes before any completed step, so starting over is the same as resuming"
)

ENTRIES: dict[str, SwitchedOff] = {
    "budget-off": SwitchedOff(
        issue=116,
        what="the check that the next call fits and the cut of max_tokens",
        edits=(
            Edit(
                AGENT,
                "            max_tokens = max_tokens_within(job.token_budget, used, bound,"
                " settings.max_tokens)\n",
                "            max_tokens = settings.max_tokens  # switched off\n",
            ),
        ),
        stack="up",
        expect=(
            Expect(
                "tests/integration/test_budget.py"
                "::test_a_job_that_loops_stops_before_its_budget_by_the_providers_count",
                "spent <= BUDGET",
            ),
        ),
    ),
    "resume-off": SwitchedOff(
        issue=117,
        what="the replay of the step log on a claim",
        edits=(
            Edit(
                AGENT,
                "    state = replay(spec.prompt, steps)\n",
                "    state = replay(spec.prompt, [])  # switched off\n",
            ),
        ),
        stack="chaos",
        expect=(
            Expect(f"{FAILPOINT}[after_claim]", None, NOTHING_TO_REPLAY),
            Expect(f"{FAILPOINT}[after_model_request]", None, NOTHING_TO_REPLAY),
            Expect(f"{FAILPOINT}[mid_stream]", None, NOTHING_TO_REPLAY),
            Expect(f"{FAILPOINT}[after_tool_intent]", "verdict(run, uninterrupted)"),
            Expect(f"{FAILPOINT}[after_tool_ran]", "verdict(run, uninterrupted)"),
            Expect(f"{FAILPOINT}[after_tool_result]", "verdict(run, uninterrupted)"),
        ),
    ),
    "unknown-check-reruns": SwitchedOff(
        issue=118,
        what="asking a person when a check cannot tell (ADR 0005)",
        edits=(
            Edit(
                "src/runtime/worker/recovery.py",
                "        return Ask(\"the tool's check could not tell"
                ' whether the call happened")\n',
                '        return Rerun("switched off: an unknown check runs the call again")\n',
            ),
        ),
        stack="chaos",
        expect=(
            Expect(
                f"{KILLS}::test_a_call_nobody_can_vouch_for_waits_for_approval_and_then_runs_again",
                'parked["status"] == "needs_approval"',
            ),
        ),
    ),
    "claim-cancel-off": SwitchedOff(
        issue=119,
        what="the claim's cancel of a job whose cancel was requested",
        edits=(
            Edit(
                "src/runtime/store/jobs.py",
                "    cancelled = jobs.c.cancel_requested\n",
                "    cancelled = and_(jobs.c.cancel_requested, literal(False))  # switched off\n",
            ),
        ),
        stack="up",
        expect=(
            Expect(
                f"{HALT}::test_a_claim_cancels_a_running_job_whose_cancel_was_requested",
                "claimed is None",
            ),
        ),
    ),
    "overdue-timer-off": SwitchedOff(
        issue=119,
        what="the workers' timer that fails jobs waiting past their deadline",
        edits=(
            Edit(
                "src/runtime/worker/loop.py",
                "                await fail_overdue(conn)\n",
                "                pass  # switched off\n",
            ),
        ),
        stack="up",
        expect=(
            Expect(
                f"{HALT}::test_the_workers_timer_fails_a_parked_job_within_seconds_of_its_deadline",
                "the timer never failed the job",
            ),
        ),
    ),
    "stop-release-off": SwitchedOff(
        issue=120,
        what="the hand-back of a job when its worker is told to stop",
        edits=(
            Edit(
                AGENT,
                "            return JobStatus.QUEUED\n",
                "            raise LeaseLost(job.id, lease.epoch)  # switched off\n",
            ),
        ),
        stack="chaos",
        expect=(
            Expect(
                "tests/chaos/test_stale_worker.py"
                "::test_a_worker_stopped_with_sigterm_hands_its_job_back",
                "another claim",
            ),
        ),
    ),
    "takeover-outlast-off": SwitchedOff(
        issue=120,
        what="the removal of an earlier lease's container, and the wait, after a takeover",
        edits=(
            Edit(
                AGENT,
                "    bound = _bound(tool, intent)\n",
                "    bound = None  # switched off\n",
            ),
        ),
        stack="chaos",
        expect=(
            Expect(
                "tests/chaos/test_sandbox_kill.py"
                "::test_a_kill_during_a_sandboxed_call_removes_the_container_and_waits",
                "left == {}",
            ),
        ),
    ),
}


def edited(edit: Edit, root: Path = ROOT) -> str:
    """The file's text with the edit made. Raises ValueError unless `old` occurs once."""
    text = (root / edit.path).read_text()
    found = text.count(edit.old)
    if found != 1:
        raise ValueError(f"{edit.path}: the edit's text occurs {found} times, not once")
    return text.replace(edit.old, edit.new)


def check(root: Path = ROOT) -> list[str]:
    """What is wrong with the list against the code in `root`. Empty when nothing is."""
    problems = []
    for name, entry in ENTRIES.items():
        for edit in entry.edits:
            try:
                compile(edited(edit, root), edit.path, "exec")
            except (ValueError, SyntaxError) as error:
                problems.append(f"{name}: {error}")
        for expect in entry.expect:
            path, _, function = expect.node.partition("::")
            function = function.partition("[")[0]
            test_file = root / path
            if not test_file.is_file() or f"def {function}(" not in test_file.read_text():
                problems.append(f"{name}: no test {expect.node}")
            elif expect.fails_with is not None and expect.fails_with not in test_file.read_text():
                problems.append(f"{name}: {path} has no {expect.fails_with!r}")
    return problems


def outcomes(junit: Path) -> dict[str, tuple[str, str]]:
    """Each test case in a junit file: node id -> (passed | failed | error | skipped, text)."""
    found: dict[str, tuple[str, str]] = {}
    for case in ET.parse(junit).getroot().iter("testcase"):  # noqa: S314 - pytest wrote it
        path = case.get("classname", "").replace(".", "/") + ".py"
        node = f"{path}::{case.get('name', '')}"
        result, text = "passed", ""
        for kind in ("failure", "error", "skipped"):
            element = case.find(kind)
            if element is not None:
                result = "failed" if kind == "failure" else kind
                text = _failing_lines(element.get("message", ""), element.text or "")
                break
        found[node] = (result, text)
    return found


def _failing_lines(message: str, traceback: str) -> str:
    """The failure's message and the statements that failed, nothing else.

    pytest's traceback shows the test's source up to the failing line, so the
    named assertion can appear in it as context while another one failed.
    pytest marks each failing statement with '>' at the start of its line.
    """
    marked = [line for line in traceback.splitlines() if line.startswith(">")]
    return "\n".join([message, *marked])


def judge(entry: SwitchedOff, found: dict[str, tuple[str, str]]) -> list[str]:
    """One line per expected test; a line that starts with "WRONG" fails the run."""
    lines = []
    for expect in entry.expect:
        result, text = found.get(expect.node, ("not run", ""))
        if expect.fails_with is None:
            ok = result == "passed"
            want = f"passed ({expect.passes_because})"
        else:
            ok = result == "failed" and expect.fails_with in text
            want = f"failed on {expect.fails_with!r}"
        lines.append(f"{'ok' if ok else 'WRONG'}: {expect.node}: {result}, expected {want}")
    return lines


def _sh(*args: str) -> None:
    print("+", " ".join(args), flush=True)
    subprocess.run(args, cwd=ROOT, check=True)  # noqa: S603 - fixed commands


def run(name: str) -> int:
    entry = ENTRIES[name]
    print(f"switched off: {name} (issue {entry.issue}): {entry.what}", flush=True)
    changed = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],  # noqa: S607
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    if changed:
        print(f"WRONG: the checkout has changes; the run would not be HEAD's code:\n{changed}")
        return 1
    _sh("git", "rev-parse", "HEAD")
    texts = {edit.path: edited(edit) for edit in entry.edits}
    for path, text in texts.items():
        (ROOT / path).write_text(text)
    _sh("git", "--no-pager", "diff", "--", *texts)
    if entry.stack == "chaos":
        (ROOT / ".chaos").mkdir(exist_ok=True)
        # The chaos workers write their records here, as with `make chaos`.
        (ROOT / ".chaos").chmod(0o777)
        compose = ["-f", "docker-compose.yml", "-f", "docker-compose.chaos.yml"]
        _sh("docker", "compose", *compose, "up", "--build", "--wait")
    else:
        _sh("make", "up")
    (ROOT / JUNIT).parent.mkdir(exist_ok=True)
    nodes = [expect.node for expect in entry.expect]
    pytest = ["uv", "run", "--frozen", "pytest", "-v", "-s", "-p", "no:cacheprovider"]
    command = [*pytest, f"--junitxml={JUNIT}", *nodes]
    # A fixed command; the node ids come from the list above.
    tests = subprocess.run(command, cwd=ROOT, check=False)  # noqa: S603
    print(f"pytest exit {tests.returncode}", flush=True)
    if not (ROOT / JUNIT).is_file():
        print("WRONG: pytest wrote no results", flush=True)
        return 1
    lines = judge(entry, outcomes(ROOT / JUNIT))
    print("\n".join(lines), flush=True)
    return 1 if any(line.startswith("WRONG") for line in lines) else 0


def main(argv: list[str]) -> int:
    match argv:
        case ["names"]:
            print("\n".join(ENTRIES))
            return 0
        case ["check"]:
            problems = check()
            print("\n".join(problems) or "every edit matches once and compiles")
            return 1 if problems else 0
        case ["run", name] if name in ENTRIES:
            return run(name)
        case ["run", _]:
            print(f"not a switched-off run; one of: {', '.join(ENTRIES)}", file=sys.stderr)
            return 2
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
