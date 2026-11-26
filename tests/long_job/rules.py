"""The pass line of the long-job suite on a real model, and what a run may start with.

One job reviews generated access records that are longer than the model's
context window, and it is judged by its outcome. This module holds the
numbers of that line, the refusals of a keyed run, the kill offsets, how a
job view is reduced to counts and one class, how a crash is read from a
worker's log, and the reviewer the fake provider plays.

Every number here is proposed until `LINE_FIXED` holds a date. Nothing in
this module talks to the stack or to a provider.
"""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from access_review import FULL as FULL_SHAPE
from access_review import SHORT as SHORT_SHAPE
from access_review import (
    Review,
    Shape,
    answer_key,
    commitment,
    generate,
    job_digest,
    parse_files,
    parse_seed,
    problems,
    read_seed,
)
from access_review.commit import SeedError

# The pass line

# The date the line was fixed. None: every number below is proposed, and no
# keyed run starts.
LINE_FIXED: str | None = None
# The most tokens the jobs of one keyed short-shape run may be given
# together. None: not set, and no such run starts.
LONG_KEYED_TOKEN_BOUND: int | None = None
# How many jobs a `counts` run has. None: not set, and no such run starts.
COUNTS_JOBS: int | None = None
# The jobs of a `must-fail` run: the control and the edited one.
MUST_FAIL_JOBS = 2

MODEL = "claude-haiku-5-5"
# The model name the fake provider's scripted reviewer answers to.
FAKE_MODEL = "fake-long-job"
MAX_TOKENS = 32_000
FILE_BYTES = 16 * 1024
# The crossing count: its floor for one job, the floor for a whole corpus,
# and the most UTF-8 bytes of one counted request.
CROSSING_FLOOR = 1_000_000
CORPUS_FLOOR = 1_200_000
CHUNK_BYTES = 512 * 1024
# Kills of a job: one at the `after_summary` failpoint and two by the
# harness, each 1 to `KILL_OFFSETS` steps after a summary.
KILLS = 3
KILL_OFFSETS = 20
# Minutes: a session's deadline, the workflow job's, and the room one build
# of the stack is given inside it.
HARNESS_MINUTES = 320
WORKFLOW_MINUTES = 350
BUILD_MINUTES = 30
# The summary trigger of the fake-provider run, in the fake's own count, and
# the seconds the fake waits inside every answer on the stack.
FAKE_TRIGGER = 20_000
DELAY = 2.0


@dataclass(frozen=True)
class Line:
    """The numbers of the line for one shape of the job."""

    name: str
    files: int
    trigger: int
    # The summaries the two harness kills follow.
    kill_after: tuple[int, int]
    # Floors: counted grants a notice makes a violation, counted grants a
    # notice clears, windows after the first summary that hold an issue, and
    # summaries (0: no floor).
    floor_made: int
    floor_cleared: int
    floor_windows: int
    floor_summaries: int
    # Whether the crossing count is part of the verdict.
    crossing: bool
    takeovers: int
    # The most tokens one job may be given, its timeout, and the minutes the
    # harness waits for it.
    budget_cap: int
    timeout_seconds: int
    job_minutes: int


FULL = Line(
    name="full",
    files=293,
    trigger=80_000,
    kill_after=(3, 7),
    floor_made=6,
    floor_cleared=2,
    floor_windows=3,
    floor_summaries=0,
    crossing=True,
    takeovers=KILLS,
    budget_cap=20_000_000,
    timeout_seconds=18_600,
    job_minutes=HARNESS_MINUTES,
)
SHORT = Line(
    name="short",
    files=36,
    trigger=20_000,
    kill_after=(2, 4),
    floor_made=6,
    floor_cleared=2,
    floor_windows=3,
    floor_summaries=6,
    crossing=False,
    takeovers=KILLS,
    budget_cap=1_500_000,
    timeout_seconds=1_800,
    job_minutes=40,
)
LINES = {line.name: line for line in (FULL, SHORT)}
SHAPES: dict[str, Shape] = {FULL.name: FULL_SHAPE, SHORT.name: SHORT_SHAPE}

# What a run does. `counts`: short jobs, a report and no verdict.
# `must-fail`: one short job, graded. `graded`: one full job, graded.
COUNTS = "counts"
MUST_FAIL = "must-fail"
GRADED = "graded"
MODES = {COUNTS: SHORT, MUST_FAIL: SHORT, GRADED: FULL}

# The first summary's text when the notices are dropped on purpose: the task
# and the rules of the prompt, then a line that starts the reading again.
# No notice, no reference code.
NOTICES_DROPPED = (
    "Review the access records. Read index.txt, then read every file it lists, in the order"
    " listed. Open one issue for each grant that is a violation.\n"
    "\n"
    "Rules:\n"
    "1. A grant is a violation when its holder has left. A person has left when a leaver"
    " notice names them, in any file, or when they are on a roster of their team and missing"
    " from a later roster of that team.\n"
    '2. A grant on a system marked "approval required" is a violation unless an approval'
    " names its holder and that system. An approval is in the file of its grant.\n"
    "3. Nothing else is a violation.\n"
    "\n"
    "Nothing is recorded as read. Continue through every file that index.txt lists, starting"
    " with the first."
)


def may_start(elapsed_seconds: float, line: Line) -> bool:
    """Whether a job submitted now ends its wait before the session's deadline."""
    return elapsed_seconds + line.job_minutes * 60 <= HARNESS_MINUTES * 60


# The refusals of a keyed run

PROVIDER = "LONG_JOB_PROVIDER"
MODE = "LONG_JOB_MODE"
SEED = "LONG_JOB_SEED"
BUDGET = "LONG_JOB_TOKEN_BUDGET"
JOBS = "LONG_JOB_JOBS"
CLAUDE = "claude"
FAKE = ("", "fake")
PUBLIC_SEED_FILE = Path("tests/unit/access_review_seed.json")
COMMITMENT_FILE = Path("tests/long_job/commitment.json")

# Each is a fixed sentence. None quotes a value.
BAD_PROVIDER = "The provider is not fake or claude."
PROPOSED = "The pass line is proposed."
BAD_MODE = "The mode is not counts, must-fail or graded."
NO_BUDGET = "The token budget is not set."
BAD_BUDGET = "The token budget is not a whole number from 1 to the cap of the job's shape."
BUDGET_GIVEN = "A must-fail run takes no token budget."
JOBS_NOT_FIXED = "The number of jobs is not fixed, or the run names another number."
NO_BOUND = "The token bound of keyed long jobs is not set, or the run is over it."
NO_SEED = "The seed is not set, or is not a seed."
PUBLIC_SEED = "The seed is the committed public seed, or that seed cannot be read."
NOT_COMMITTED = "The commitment is not set, or is not the seed's."
COMMITTED = "The seed is the one the commitment is made to."
CHECKS = "The seed's job fails its checks."

_DIGITS = re.compile(r"[0-9]{1,18}")


def _budget(env: Mapping[str, str], mode: str) -> tuple[int | None, str | None]:
    """The tokens one job of the run is given, or the sentence that refuses the run."""
    given = env.get(BUDGET, "")
    line = MODES[mode]
    if mode == MUST_FAIL:
        return (None, BUDGET_GIVEN) if given else (line.budget_cap, None)
    if not given:
        return None, NO_BUDGET
    if not _DIGITS.fullmatch(given) or not 1 <= int(given) <= line.budget_cap:
        return None, BAD_BUDGET
    return int(given), None


def _bound(env: Mapping[str, str], mode: str, budget: int | None) -> list[str]:
    """The refusals of a short-shape run for its number of jobs and their tokens together."""
    if mode == GRADED:
        return []
    found = []
    jobs: int | None = MUST_FAIL_JOBS
    if mode == COUNTS:
        jobs = COUNTS_JOBS
        if jobs is None or env.get(JOBS, "") != str(jobs):
            found.append(JOBS_NOT_FIXED)
    bound = LONG_KEYED_TOKEN_BOUND
    if bound is None or (jobs is not None and budget is not None and jobs * budget > bound):
        found.append(NO_BOUND)
    return found


def _committed(root: Path) -> str | None:
    """The commitment to the full shape's job, or None when none is set or readable."""
    try:
        data = json.loads((root / COMMITMENT_FILE).read_bytes())
    except OSError, ValueError:
        return None
    value = data.get(FULL.name) if isinstance(data, dict) else None
    return value if isinstance(value, str) else None


def _commitment(seed: bytes) -> str | None:
    """The commitment of `seed` for the full shape, or None when its job cannot be made."""
    try:
        review = generate(seed, FULL_SHAPE)
        return commitment(seed, FULL_SHAPE, answer_key(review), job_digest(review))
    except Exception:
        # No text of the error: it could quote the seed or a record.
        return None


def _fails_checks(seed: bytes, shape: Shape) -> bool:
    try:
        return bool(problems(generate(seed, shape)))
    except Exception:
        return True


def _seed_refusals(seed: bytes, mode: str, root: Path) -> list[str]:
    found = []
    try:
        public: bytes | None = read_seed(root / PUBLIC_SEED_FILE)
    except SeedError:
        public = None
    if public is None or seed == public:
        found.append(PUBLIC_SEED)
    committed = _committed(root)
    if mode == GRADED:
        if committed is None or committed != _commitment(seed):
            found.append(NOT_COMMITTED)
    elif committed is not None and committed == _commitment(seed):
        found.append(COMMITTED)
    if _fails_checks(seed, SHAPES[MODES[mode].name]):
        found.append(CHECKS)
    return found


def refusals(env: Mapping[str, str], root: Path) -> list[str]:
    """Why a keyed run may not start, as fixed sentences. Empty on the fake provider.

    A provider that is neither the fake (unset, empty or `fake`) nor `claude`
    is refused, so a misspelt provider never passes as the fake.

    `env` is the run's environment and `root` the checkout. The seed is read
    from `env` alone and is in no sentence. The seed's own refusals are left
    out while the mode or the seed itself is refused.
    """
    provider = env.get(PROVIDER, "")
    if provider in FAKE:
        return []
    if provider != CLAUDE:
        return [BAD_PROVIDER]
    found = []
    if LINE_FIXED is None:
        found.append(PROPOSED)
    mode = env.get(MODE, "")
    if mode not in MODES:
        return [*found, BAD_MODE]
    budget, refused = _budget(env, mode)
    if refused is not None:
        found.append(refused)
    found += _bound(env, mode, budget)
    try:
        seed = parse_seed(env.get(SEED, ""))
    except SeedError:
        # The error's text is a fixed sentence too, but only this one is given.
        return [*found, NO_SEED]
    return found + _seed_refusals(seed, mode, root)


# Kills


def offset(seed: bytes, shape: str, i: int) -> int:
    """How many steps after its summary harness kill `i` (2 or 3) aims, from 1 to 20.

    Drawn from the seed and the shape's name alone, with SHA-256. A value
    that would make some offsets likelier than others is drawn again.
    """
    limit = (2**64 // KILL_OFFSETS) * KILL_OFFSETS
    for n in range(256):
        block = b"long-job-kill\x00" + shape.encode() + b"\x00" + bytes([i, n]) + seed
        value = int.from_bytes(hashlib.sha256(block).digest()[:8], "big")
        if value < limit:
            return 1 + value % KILL_OFFSETS
    raise ValueError("no offset was drawn")


# What a job view is reduced to

# The runtime's words for a failed job. The two of the store are whole
# texts; the others are how a text starts.
DEADLINE_ERROR = "the job's deadline passed"
TAKEOVER_CAP_ERROR = "the job was taken over 5 times after its worker stopped; it is not run again"
MESSAGE_MISMATCH_ERROR = "the summary is not usable: its message does not match its text"
_WHOLE = {
    DEADLINE_ERROR: "deadline",
    TAKEOVER_CAP_ERROR: "takeover_cap",
    MESSAGE_MISMATCH_ERROR: "message_mismatch",
}
# In order: the first start that fits names the class.
_STARTS = (
    ("model call failed: provider answered 429: ", "spend_cap"),
    (
        "model call failed: provider sent an error during the stream: rate_limit_error: ",
        "stream_spend_cap",
    ),
    ("model call failed: gave up at the cap of ", "provider_error_cap"),
    ("model call failed: ", "provider_error"),
    ("the spec asks for approval before tools this worker does not offer: ", "not_offered"),
    ("the summary is not usable: ", "summary_problem"),
    ("the model's response is not usable: ", "response_problem"),
    ("the summary leaves the next window at or over the trigger: ", "oversize"),
    ("the job repeated the same call to ", "repeat"),
    ("the token budget does not cover the next model call: ", "budget"),
    ("the model's response was cut off at max_tokens", "max_tokens"),
    ("the model declined the request", "refusal"),
    ("unexpected stop_reason: ", "stop_reason"),
)
ERROR_CLASSES = ("none", *_WHOLE.values(), *(name for _, name in _STARTS), "other")


def error_class(error: object) -> str:
    """One name of `ERROR_CLASSES` for a job's error text. The text itself is not kept."""
    if error is None:
        return "none"
    if not isinstance(error, str):
        return "other"
    if error in _WHOLE:
        return _WHOLE[error]
    return next((name for start, name in _STARTS if error.startswith(start)), "other")


def reduced(job: Mapping[str, Any]) -> dict[str, Any]:
    """A job view, from `GET /jobs/{id}` or the job list, without its spec and its error text.

    What is kept are counts, times, the status, the worker that holds the
    lease, the class of the error and the number of an open question. A key
    the view lacks is None.
    """
    question = job.get("question")
    return {
        "status": job.get("status"),
        "takeovers": job.get("takeovers"),
        "lease_owner": job.get("lease_owner"),
        "lease_expires_at": job.get("lease_expires_at"),
        "tokens_used": job.get("tokens_used"),
        "deadline": job.get("deadline"),
        "error": error_class(job.get("error")),
        "question": question.get("seq") if isinstance(question, Mapping) else None,
    }


# What a worker logs when a job ends on an error the worker did not expect.
CRASHED = "job crashed"


def _log_entry(line: str) -> dict[str, Any] | None:
    """The JSON object of a log line, with or without Docker's timestamp before it."""
    text = line if line.startswith("{") else line.partition(" ")[2]
    try:
        entry = json.loads(text)
    except ValueError:
        return None
    return entry if isinstance(entry, dict) else None


def crashed(log_text: str, job_id: str) -> bool:
    """Whether a worker's log holds the crash record of the job.

    Only a line whose own message is the crash record and whose own job id
    is the job's counts. The words inside another field show nothing: a
    traceback and a job's error are written from job content.
    """
    for line in log_text.splitlines():
        entry = _log_entry(line)
        if entry and entry.get("message") == CRASHED and entry.get("job_id") == job_id:
            return True
    return False


# The fake provider's reviewer

SUMMARY_TEXT = (
    "The task: review the access records and open an issue for each grant that is a"
    " violation. Read so far: every file before the turn named below."
)
LAST_TURN = "Every file is read and every violation has an issue."


def _calls(name: str, inputs: list[dict[str, str]], delay_seconds: float) -> dict[str, Any]:
    turn: dict[str, Any] = {
        "content": [{"type": "tool_use", "name": name, "input": given} for given in inputs]
    }
    if delay_seconds:
        turn["delay_seconds"] = delay_seconds
    return turn


def scripted_reviewer(review: Review, *, delay_seconds: float = 0.0) -> dict[str, Any]:
    """A fake-provider scenario that reviews `review` as its answer key says.

    It reads the index, then each file in index order, one read a turn.
    After a file with violations it opens one issue for each, in one turn,
    with the grant's id and the reference code in the title. Then it says it
    is done. The key is the one over the files and the notices. Every turn
    and every summary waits `delay_seconds` before its answer.
    """
    violations = set(answer_key(review).key_full)
    code = review.reference_code
    turns = []
    for path, text in review.files.items():
        turns.append(_calls("read_file", [{"path": path}], delay_seconds))
        found = [
            grant.id
            for file in parse_files({path: text}).files
            for grant in file.grants
            if grant.id in violations
        ]
        if found:
            issues = [
                {"title": f"{grant}: access to remove, {code}", "body": f"Found in {path}."}
                for grant in found
            ]
            turns.append(_calls("create_issue", issues, delay_seconds))
    last: dict[str, Any] = {"content": [{"type": "text", "text": LAST_TURN}]}
    summary: dict[str, Any] = {"text": SUMMARY_TEXT}
    if delay_seconds:
        last["delay_seconds"] = summary["delay_seconds"] = delay_seconds
    return {
        "description": "A reviewer that reads every file and opens an issue for each violation.",
        "turns": [*turns, last],
        "summary": summary,
    }
