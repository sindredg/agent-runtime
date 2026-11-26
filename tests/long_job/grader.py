"""How a long job on a real model is judged: its clauses, its verdict, and what is only reported.

`grade` reads one job's reduced view, its steps, the tracker's issues for
it and what the harness saw, against the answers of the generated review.
It reports every clause that holds, each with a count, and one verdict:

1. FAIL_RUNTIME, the runtime's own failure: verify failed (R2), a tool list
   or an unknown-tool answer is wrong (R5), an operation id has too many
   issues (R6), a worker crashed on the job, or the job failed on a
   runtime error. It wins over a void, which is still listed.
2. VOID: the run shows nothing either way.
3. NO_CLASS: the job did not succeed and its failure has no class yet.
4. FAIL, the job's: it stopped in one of five ways (R1), the crossing
   count is under its floor (R3), a counted violation has no issue (R7), a
   counted cleared grant has one (R8), or an issue after the first summary
   lacks the reference code (R9).
5. INCONCLUSIVE: a floor is missed, a kill did not land, the count is
   missing, verify could not tell, or grading raised.
6. PASS.

`counts` is the report of a run with no verdict. Nothing here talks to the
stack, and nothing returned holds a title, an id or any other text of a job.
"""

import hashlib
import itertools
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Protocol

from access_review import Review, answer_key, parse_files

VERDICTS = ("FAIL_RUNTIME", "VOID", "NO_CLASS", "FAIL", "INCONCLUSIVE", "PASS")
FAIL_RUNTIME, VOID, NO_CLASS, FAIL, INCONCLUSIVE, PASS = VERDICTS

# What the harness can name as the cause of a void run.
HARNESS_VOIDS = ("clock", "tracker_restart", "harness_error")
VOID_CLAUSES = tuple(f"void_{cause}" for cause in (*HARNESS_VOIDS, "no_model_request", "spend_cap"))
RUNTIME_CLAUSES = ("R2", "R5", "R6", "crash", "runtime")
JOB_CLAUSES = ("R1", "R3", "R7", "R8", "R9")
OPEN_CLAUSES = (
    "floor_made",
    "floor_cleared",
    "floor_windows",
    "floor_summaries",
    "kill_failpoint",
    "kill_2",
    "kill_3",
    "kill_windows",
    "takeovers",
    "count_missing",
    "count_failed",
    "count_partial",
    "verify_2",
    "verify_unknown",
    "grading_error",
)
# Every clause, in the order it is reported: the order of the verdicts.
CLAUSES = (*RUNTIME_CLAUSES, *VOID_CLAUSES, "no_class", *JOB_CLAUSES, *OPEN_CLAUSES)

# The classes of a job that did not succeed. The five of R1: two are read
# from the last answer's stop reason, three from the job's reduced error.
STOP_REASONS = ("refusal", "max_tokens")
STOP_ERRORS = ("deadline", "takeover_cap", "provider_error_cap")
R1_CLASSES = (*STOP_REASONS, *STOP_ERRORS)
# The reduced errors that are a runtime error by themselves.
RUNTIME_ERRORS = ("not_offered", "message_mismatch")
SPEND_CAPS = ("spend_cap", "stream_spend_cap")
# Every class a reduced job's error can have, and every status of a job.
# Where a job stopped, with no class or at a spend cap, is one of these names, or `other`.
ERRORS = (
    "none",
    *STOP_ERRORS,
    *RUNTIME_ERRORS,
    *SPEND_CAPS,
    "provider_error",
    "summary_problem",
    "response_problem",
    "oversize",
    "repeat",
    "budget",
    "max_tokens",
    "refusal",
    "stop_reason",
    "other",
)
STATUSES = (
    "queued",
    "running",
    "waiting",
    "needs_approval",
    "succeeded",
    "failed",
    "cancelled",
)
NOT_STORED = "it cannot be stored"
EMPTY_SUMMARY = "it has no text"

TOOLS = ("read_file", "create_issue")
UNKNOWN_TOOL = "unknown tool: {name}"
# A tracker's refusal of an issue, as the tool reports it.
REFUSED_TITLE = re.compile(r"the tracker answered 4[0-9]{2}")
# A title is split into tokens at everything but ASCII letters and digits.
_NOT_TOKEN = re.compile(r"[^A-Za-z0-9]+")
ANSWERS = ("model_response", "summary")
FINAL = ("succeeded", "failed", "cancelled")
# Reported: requests with more input tokens than this, and a summary with
# more output tokens than that.
LARGE_REQUEST = 100_000
LARGE_SUMMARY = 16_000

type Steps = Sequence[dict[str, Any]]


class Shape(Protocol):
    """The numbers of the line that depend on the job's shape."""

    @property
    def floor_made(self) -> int: ...
    @property
    def floor_cleared(self) -> int: ...
    @property
    def floor_windows(self) -> int: ...
    @property
    def floor_summaries(self) -> int: ...
    @property
    def crossing(self) -> bool: ...
    @property
    def takeovers(self) -> int: ...


class Counted(Protocol):
    """A crossing count."""

    @property
    def total(self) -> int: ...
    @property
    def chunks(self) -> int: ...
    @property
    def partial(self) -> bool: ...


@dataclass(frozen=True)
class Kill:
    """A kill the harness made: the seq it aimed at and the lease epoch it ended.

    `held`: the killed container held the job's lease. `problem`: why the
    kill was not made, if it was not.
    """

    target: int | None
    epoch: int | None
    held: bool
    latency: float | None = None
    problem: str | None = None


@dataclass(frozen=True)
class Log:
    """One job as the harness saw it."""

    # The job view, reduced: a status, counts and the class of its error.
    job: Mapping[str, Any]
    steps: Steps
    # The tracker's issues with the job's operation ids.
    issues: Sequence[Mapping[str, Any]]
    # The questions a person approved, each with its `operation_id`.
    questions: Sequence[Mapping[str, Any]]
    # The harness kills, in order: kill 2, then kill 3.
    kills: Sequence[Kill]
    # How often a worker logged that it stopped at the failpoint.
    failpoint_lines: int
    # The exit status of `runtime verify`.
    verify: int | None
    count: Counted | None
    count_failed: bool = False
    # Causes of `HARNESS_VOIDS`.
    voids: tuple[str, ...] = ()
    # A worker's log holds the crash record of the job.
    crashed: bool = False
    seconds: float | None = None


@dataclass(frozen=True)
class Answers:
    """What a review's answer key says, with file contents as hashes."""

    # Grants a notice makes a violation, and grants a notice clears.
    made: tuple[str, ...]
    cleared: tuple[str, ...]
    # Every violation, over the files and the notices.
    violations: frozenset[str]
    # Every grant id of the organisation.
    grants: frozenset[str]
    code: str
    # The SHA-256 of the file each decided grant is in: `made`, `cleared`
    # and the carried ones.
    files: Mapping[str, str]
    # A grant decided by a fact in an earlier file, and that file's SHA-256.
    carried: tuple[tuple[str, str], ...]
    # Every corpus file: its SHA-256 and its bytes.
    corpus: Mapping[str, int]


@dataclass(frozen=True)
class Graded:
    verdict: str
    # Every clause that holds, with how often, in the order of `CLAUSES`.
    clauses: dict[str, int]
    # What each floor was measured as.
    floors: dict[str, int]
    # Reported, not asserted.
    counts: dict[str, Any] = field(default_factory=dict)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def answers(review: Review) -> Answers:
    """The answers of a generated review."""
    key = answer_key(review)
    # The index is the first file. The corpus follows in index order.
    corpus = list(review.files.items())[1:]
    where = {
        grant.id: sha256(text)
        for path, text in corpus
        for file in parse_files({path: text}).files
        for grant in file.grants
    }
    carried = [planted for planted in review.planted if planted.kind == "carried"]
    decided = (*key.made, *key.cleared, *(planted.grant.id for planted in carried))
    return Answers(
        made=key.made,
        cleared=key.cleared,
        violations=frozenset(key.key_full),
        grants=frozenset(where),
        code=key.reference_code,
        files={grant: where[grant] for grant in decided},
        carried=tuple(
            (planted.grant.id, sha256(corpus[planted.fact_row][1]))
            for planted in carried
            if planted.fact_row is not None
        ),
        corpus={sha256(text): len(text.encode()) for _, text in corpus},
    )


# The steps


def in_order(log: Log) -> Log:
    """The log with its steps in seq order, whatever order they came in."""
    return replace(log, steps=sorted(log.steps, key=lambda step: step["seq"]))


def of_kind(steps: Steps, *kinds: str) -> list[dict[str, Any]]:
    return [step for step in steps if step["kind"] in kinds]


def window_of(seq: int, summaries: Sequence[int]) -> int:
    """The window a step is in: the number of `summary` steps before it."""
    return sum(summary < seq for summary in summaries)


def reads(steps: Steps, corpus: Mapping[str, int] | None = None) -> dict[str, list[int]]:
    """The seqs of the reads of each content, by its SHA-256, in order.

    A read is a `tool_result` that is not an error. With `corpus`, only
    content that is a corpus file.
    """
    found: dict[str, list[int]] = {}
    for step in of_kind(steps, "tool_result"):
        content = step["payload"].get("content")
        if step["payload"].get("is_error") is not False or not isinstance(content, str):
            continue
        digest = sha256(content)
        if corpus is None or digest in corpus:
            found.setdefault(digest, []).append(step["seq"])
    return found


def holds_code(title: str, code: str) -> bool:
    """Whether a title holds the reference code as it is, between characters that are not ASCII
    letters or digits."""
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(code)}(?![A-Za-z0-9])", title) is not None


def held_ids(title: str, grants: frozenset[str]) -> set[str]:
    """The known grant ids a title holds, each as a whole token."""
    return set(_NOT_TOKEN.split(title)) & grants


@dataclass(frozen=True)
class Filed:
    """An issue of the job: the ids its title holds, and where its call is in the log."""

    operation: str
    ids: frozenset[str]
    has_code: bool
    # The seq of the call's first `tool_intent`. None: the log has no such call.
    place: int | None

    @property
    def grant(self) -> str | None:
        """The grant the issue is for: the one known id of a well-formed title."""
        return next(iter(self.ids)) if len(self.ids) == 1 else None


def filed(log: Log, known: Answers) -> list[Filed]:
    intents: dict[str, int] = {}
    for step in of_kind(log.steps, "tool_intent"):
        intents.setdefault(str(step["payload"].get("tool_use_id")), step["seq"])
    found = []
    for issue in log.issues:
        operation, title = str(issue["operation_id"]), str(issue["title"])
        found.append(
            Filed(
                operation=operation,
                ids=frozenset(held_ids(title, known.grants)),
                has_code=holds_code(title, known.code),
                place=intents.get(operation.partition(":")[2]),
            )
        )
    return found


def counted(
    grants: Sequence[str], known: Answers, log: Log, first_summary: int | None
) -> list[str]:
    """The grants whose file was first read after the job's first summary."""
    if first_summary is None:
        return []
    read = reads(log.steps)
    return [
        grant
        for grant in grants
        if known.files.get(grant) in read and read[known.files[grant]][0] > first_summary
    ]


# The class of a job that did not succeed


def last_answer(steps: Steps) -> dict[str, Any]:
    """The payload of the last `model_response` or `summary` step, or an empty one."""
    found = of_kind(steps, *ANSWERS)
    return found[-1]["payload"] if found else {}


def job_class(log: Log) -> tuple[str | None, str | None]:
    """(the class, where it stopped). (None, None) for a job that succeeded.

    First match wins. A crash record counts whatever the job ended as. Where
    it stopped is named for a job with no class, and for a spend cap: the
    form of the cap. The stop reason of the last answer is read from its
    stored step, before the job's reduced error.
    """
    status, error = log.job.get("status"), log.job.get("error")
    if log.crashed:
        return "crash", None
    if status == "succeeded":
        return None, None
    if status == "failed" and error in SPEND_CAPS:
        return "spend_cap", str(error)
    answer = last_answer(log.steps)
    if answer.get("stop_reason") in STOP_REASONS:
        return str(answer["stop_reason"]), None
    if error in STOP_ERRORS:
        return str(error), None
    if error in RUNTIME_ERRORS or str(answer.get("problem", "")).startswith(NOT_STORED):
        return "runtime", None
    site = error if status == "failed" else status
    known = ERRORS if status == "failed" else STATUSES
    return "no_class", str(site) if site in known else "other"


# The clauses


def tool_list_problems(steps: Steps) -> int:
    """R5: request steps whose tools are not the two, and other tools not answered as unknown."""
    found = 0
    for step in of_kind(steps, "model_request"):
        tools = step["payload"].get("tools")
        if not isinstance(tools, list) or sorted(map(str, tools)) != sorted(TOOLS):
            found += 1
    results = {
        step["payload"].get("tool_use_id"): step["payload"].get("content")
        for step in of_kind(steps, "tool_result")
    }
    for step in of_kind(steps, "model_response"):
        content = step["payload"].get("content")
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name, call = block.get("name"), block.get("id")
            if name in TOOLS or call not in results:
                continue
            if results[call] != UNKNOWN_TOOL.format(name=name):
                found += 1
    return found


def repeated_operations(log: Log) -> int:
    """R6: operation ids with more issues than one and the reruns a person approved."""
    issues: dict[str, int] = {}
    for issue in log.issues:
        issues[str(issue["operation_id"])] = issues.get(str(issue["operation_id"]), 0) + 1
    approved: dict[str, int] = {}
    for question in log.questions:
        operation = str(question.get("operation_id"))
        approved[operation] = approved.get(operation, 0) + 1
    return sum(count > 1 + approved.get(operation, 0) for operation, count in issues.items())


def kill_report(log: Log, summaries: Sequence[int]) -> list[dict[str, Any]]:
    """Each harness kill: its target, its worker's last step, that step's window, and if it landed.

    Landed: the killed container held the lease, and its worker's last step
    is at or past the target and after the first summary.
    """
    found = []
    for kill in log.kills:
        last = max(
            (step["seq"] for step in log.steps if step["lease_epoch"] == kill.epoch), default=None
        )
        landed = (
            kill.problem is None
            and kill.held
            and kill.target is not None
            and last is not None
            and bool(summaries)
            and last >= kill.target
            and last > summaries[0]
        )
        found.append(
            {
                "target": kill.target,
                "last": last,
                "window": None if last is None else window_of(last, summaries),
                "latency": kill.latency,
                "landed": landed,
            }
        )
    return found


def _clauses(found: Mapping[str, int]) -> dict[str, int]:
    return {name: found[name] for name in CLAUSES if found.get(name)}


def _verdict(clauses: Mapping[str, int]) -> str:
    for verdict, names in (
        (FAIL_RUNTIME, RUNTIME_CLAUSES),
        (VOID, VOID_CLAUSES),
        (NO_CLASS, ("no_class",)),
        (FAIL, JOB_CLAUSES),
        (INCONCLUSIVE, OPEN_CLAUSES),
    ):
        if any(name in clauses for name in names):
            return verdict
    return PASS


def grade(log: Log, known: Answers, *, shape: Shape, crossing_floor: int) -> Graded:
    """The verdict on one job, every clause that holds, the floors and the reported counts."""
    log = in_order(log)
    found: dict[str, int] = {}
    steps = log.steps
    summaries = [step["seq"] for step in of_kind(steps, "summary")]
    first_summary = summaries[0] if summaries else None
    issues = filed(log, known)
    status = log.job.get("status")

    # Void
    for cause in log.voids:
        if cause not in HARNESS_VOIDS:
            raise ValueError("not a cause of a void run")
        found[f"void_{cause}"] = 1
    if status == "failed" and not of_kind(steps, "model_request"):
        found["void_no_model_request"] = 1
    # Read apart from the class: a crash record beside a spend cap lists both.
    found["void_spend_cap"] = int(status == "failed" and log.job.get("error") in SPEND_CAPS)
    name, site = job_class(log)
    # Beside a crash the record still names the cap's form.
    if found["void_spend_cap"] and name == "crash":
        site = str(log.job.get("error"))

    # The runtime's own failures
    found["R2"] = int(log.verify == 1)
    found["R5"] = tool_list_problems(steps)
    found["R6"] = repeated_operations(log)
    found["crash"] = int(name == "crash")
    found["runtime"] = int(name == "runtime")
    found["no_class"] = int(name == "no_class")

    # The job's failures
    found["R1"] = int(name in R1_CLASSES)
    count = log.count
    if shape.crossing and count is not None and not count.partial:
        found["R3"] = int(count.total < crossing_floor)
    made = counted(known.made, known, log, first_summary)
    cleared = counted(known.cleared, known, log, first_summary)
    with_issue = {issue.grant for issue in issues}
    held = {grant for issue in issues for grant in issue.ids}
    found["R7"] = sum(grant not in with_issue for grant in made)
    found["R8"] = sum(grant in held for grant in cleared)
    late = [
        issue
        for issue in issues
        if issue.place is not None and first_summary is not None and issue.place > first_summary
    ]
    found["R9"] = sum(not issue.has_code for issue in late)

    # Inconclusive
    windows = {window_of(issue.place, summaries) for issue in late if issue.place and issue.grant}
    floors = {
        "made": len(made),
        "cleared": len(cleared),
        "windows": len(windows),
        "summaries": len(summaries),
    }
    for floor, measured in floors.items():
        found[f"floor_{floor}"] = int(measured < getattr(shape, f"floor_{floor}"))
    kills = kill_report(log, summaries)
    found["kill_failpoint"] = int(log.failpoint_lines != 1)
    for number, clause in enumerate(("kill_2", "kill_3")):
        found[clause] = int(number >= len(kills) or not kills[number]["landed"])
    landed = [kill["window"] for kill in kills[:2] if kill["landed"]]
    found["kill_windows"] = int(len(landed) == 2 and landed[0] == landed[1])
    takeovers = log.job.get("takeovers")
    found["takeovers"] = int(not isinstance(takeovers, int) or takeovers < shape.takeovers)
    if shape.crossing:
        found["count_failed"] = int(log.count_failed)
        found["count_missing"] = int(count is None and not log.count_failed)
        found["count_partial"] = int(count is not None and count.partial)
    found["verify_2"] = int(log.verify == 2)
    found["verify_unknown"] = int(log.verify not in (0, 1, 2))

    clauses = _clauses(found)
    reported = {
        "class": name,
        "site": site,
        "kills": kills,
        "crossing": None
        if count is None
        else {"total": count.total, "chunks": count.chunks, "partial": count.partial},
        **report(log, known, issues, kills),
    }
    return Graded(_verdict(clauses), clauses, floors, reported)


def ungraded(voids: Sequence[str], *, crashed: bool, verify: int | None) -> Graded:
    """The verdict on a job whose grading raised: its voids, `grading_error`, and what the
    harness saw without grading, a crash record and verify's exit 1.

    No clause of the job's log can be known. A runtime failure still wins, then a void.
    """
    found = {f"void_{cause}": 1 for cause in voids if cause in HARNESS_VOIDS}
    found |= {"crash": int(crashed), "R2": int(verify == 1)}
    clauses = _clauses({**found, "grading_error": 1})
    return Graded(_verdict(clauses), clauses, {}, {})


# Reported, not asserted


def title_counts(log: Log, known: Answers, issues: Sequence[Filed]) -> dict[str, int]:
    """Found, missed and false issues, titles of no one grant, repeats, and refused titles.

    Two issues under one operation id count once.
    """
    once = {issue.operation: issue for issue in issues}.values()
    grants = [issue.grant for issue in once if issue.grant is not None]
    creates = {
        step["payload"].get("tool_use_id")
        for step in of_kind(log.steps, "tool_intent")
        if step["payload"].get("name") == "create_issue"
    }
    refused = sum(
        step["payload"].get("tool_use_id") in creates
        and step["payload"].get("is_error") is True
        and bool(REFUSED_TITLE.fullmatch(str(step["payload"].get("content"))))
        for step in of_kind(log.steps, "tool_result")
    )
    return {
        "found": len(set(grants) & known.violations),
        "missed": len(known.violations - set(grants)),
        "false": len(set(grants) - known.violations),
        "malformed": sum(issue.grant is None for issue in once),
        "repeated": len(grants) - len(set(grants)),
        "refused": refused,
    }


def rereads(steps: Steps, known: Answers) -> int:
    """Reads of a corpus file after its first, over the whole job."""
    return sum(len(seqs) - 1 for seqs in reads(steps, known.corpus).values())


def file_recall(log: Log, known: Answers, issues: Sequence[Filed]) -> dict[str, int]:
    """How many carried facts the job had to remember, and how many of them it used.

    In the class: the fact's file was first read before the last summary
    that precedes the first read of the grant's file, and not again from
    that summary to the grant's issue, or to the end with no issue. Used:
    in the class, and the grant has an issue.
    """
    read = reads(log.steps)
    summaries = [step["seq"] for step in of_kind(log.steps, "summary")]
    in_class = used = 0
    for grant, fact in known.carried:
        grant_reads, fact_reads = read.get(known.files[grant], []), read.get(fact, [])
        before = [summary for summary in summaries if grant_reads and summary < grant_reads[0]]
        if not before or not fact_reads or fact_reads[0] > before[-1]:
            continue
        places = [issue.place for issue in issues if issue.grant == grant and issue.place]
        end = min(places) if places else None
        if any(before[-1] < seq and (end is None or seq < end) for seq in fact_reads):
            continue
        in_class += 1
        used += int(end is not None)
    return {"in_class": in_class, "used": used, "rereads": rereads(log.steps, known)}


def usage(step: dict[str, Any], name: str) -> int:
    found = step["payload"].get("usage")
    value = found.get(name) if isinstance(found, dict) else None
    return value if isinstance(value, int) else 0


def refusals(steps: Steps) -> dict[str, int]:
    """Answers the model declined, by category. `none` for one with no category."""
    found: dict[str, int] = {}
    for step in of_kind(steps, *ANSWERS):
        refusal = step["payload"].get("refusal")
        if step["payload"].get("stop_reason") != "refusal" and not isinstance(refusal, dict):
            continue
        category = refusal.get("category") if isinstance(refusal, dict) else None
        name = category if isinstance(category, str) else "none"
        found[name] = found.get(name, 0) + 1
    return found


def report(
    log: Log, known: Answers, issues: Sequence[Filed], kills: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    steps = log.steps
    answered = of_kind(steps, *ANSWERS)
    read = reads(steps, known.corpus)
    size = sum(known.corpus[digest] for digest in read)
    whole = sum(known.corpus.values())
    takeovers = log.job.get("takeovers")
    taken = takeovers if isinstance(takeovers, int) else 0
    harness = sum(bool(kill["landed"]) for kill in kills)
    inputs = [usage(step, "input_tokens") for step in answered]
    return {
        "titles": title_counts(log, known, issues),
        "recall": file_recall(log, known, issues),
        "refusals": refusals(steps),
        "files": {
            "read": len(read),
            "never_read": len(known.corpus) - len(read),
            "bytes": size,
            "share": size / whole if whole else 0.0,
        },
        "tokens": {
            "input": sum(inputs),
            "output": sum(usage(step, "output_tokens") for step in answered),
            "thinking": sum(usage(step, "thinking_tokens") for step in answered),
            "used": log.job.get("tokens_used"),
        },
        "summaries": len(of_kind(steps, "summary")),
        "largest_request": max(inputs, default=0),
        "requests_over": sum(tokens > LARGE_REQUEST for tokens in inputs),
        "approvals": len(log.questions),
        "takeovers": {
            "failpoint": log.failpoint_lines,
            "harness": harness,
            "other": max(0, taken - log.failpoint_lines - harness),
        },
        "seconds": log.seconds,
        "unplaced": sum(issue.place is None for issue in issues),
    }


# The report with no verdict


def window_tokens(steps: Steps) -> tuple[int, int]:
    """(tokens, bytes) that tool results added to the input, summed over the windows.

    For a window: the input tokens of its last response less those of its
    first, less the output tokens other than thinking of every response
    before the last, over the UTF-8 bytes of the tool results between the
    two. A window with fewer than two responses is left out. The tokens also
    hold what a request adds around each result, and thinking is never
    subtracted whether or not it is sent again, so tokens over bytes is at
    most the tokens a byte of a result costs.
    """
    tokens = size = 0
    window: list[dict[str, Any]] = []
    for step in [*steps, {"kind": "summary", "seq": 0, "payload": {}}]:
        if step["kind"] != "summary":
            window.append(step)
            continue
        responses = of_kind(window, "model_response")
        if len(responses) >= 2:
            first, last = responses[0], responses[-1]
            tokens += usage(last, "input_tokens") - usage(first, "input_tokens")
            tokens -= sum(
                usage(response, "output_tokens") - usage(response, "thinking_tokens")
                for response in responses[:-1]
            )
            size += sum(
                len(str(result["payload"].get("content", "")).encode())
                for result in of_kind(window, "tool_result")
                if first["seq"] < result["seq"] < last["seq"]
            )
        window = []
    return tokens, size


def calls(steps: Steps) -> tuple[int, float]:
    """(calls, seconds): each `model_request` whose answer is the next step, of the same worker."""
    count, seconds = 0, 0.0
    for request, answer in itertools.pairwise(steps):
        if request["kind"] != "model_request" or answer["kind"] not in ANSWERS:
            continue
        if request["lease_epoch"] != answer["lease_epoch"]:
            continue
        asked = datetime.fromisoformat(request["created_at"])
        count += 1
        seconds += (datetime.fromisoformat(answer["created_at"]) - asked).total_seconds()
    return count, seconds


def reads_asked(step: dict[str, Any]) -> int:
    content = step["payload"].get("content")
    return sum(
        isinstance(block, dict)
        and block.get("type") == "tool_use"
        and block.get("name") == "read_file"
        for block in (content if isinstance(content, list) else [])
    )


def _per_byte(tokens: int, size: int) -> float | None:
    return tokens / size if size else None


def job_counts(log: Log, known: Answers) -> dict[str, Any]:
    steps = in_order(log).steps
    tokens, size = window_tokens(steps)
    made, seconds = calls(steps)
    responses = of_kind(steps, "model_response")
    asked = [reads_asked(step) for step in responses]
    summaries = of_kind(steps, "summary")
    largest = max(summaries, key=lambda step: usage(step, "output_tokens"), default=None)
    output = None if largest is None else usage(largest, "output_tokens")
    return {
        "status": log.job.get("status"),
        "refusals": refusals(steps),
        "summaries": len(summaries),
        "empty_summaries": sum(s["payload"].get("problem") == EMPTY_SUMMARY for s in summaries),
        "window_tokens": tokens,
        "window_bytes": size,
        "tokens_per_byte_at_most": _per_byte(tokens, size),
        "calls": made,
        "call_seconds": seconds,
        "responses": len(responses),
        "reads": sum(asked),
        "most_reads": max(asked, default=0),
        "rereads": rereads(steps, known),
        "largest_summary": output,
        "largest_summary_thinking": None if largest is None else usage(largest, "thinking_tokens"),
        "summary_over": output is not None and output > LARGE_SUMMARY,
    }


def counts(logs: Sequence[Log], known: Answers) -> dict[str, Any]:
    """The report of a run with no verdict: each job's counts, and their sum.

    Refusals by category, summaries and empty ones, tokens per byte of a
    tool result (an upper bound), seconds per call, reads per response,
    rereads, and the largest summary with a flag when it is over 16,000
    output tokens. No kill is made in such a run and none is reported.
    """
    jobs = [job_counts(log, known) for log in logs]
    categories: dict[str, int] = {}
    for job in jobs:
        for category, number in job["refusals"].items():
            categories[category] = categories.get(category, 0) + number
    summed = {
        name: sum(job[name] for job in jobs)
        for name in (
            "summaries",
            "empty_summaries",
            "window_tokens",
            "window_bytes",
            "calls",
            "call_seconds",
            "responses",
            "reads",
            "rereads",
        )
    }
    largest = max(
        (job for job in jobs if job["largest_summary"] is not None),
        key=lambda job: job["largest_summary"],
        default=None,
    )
    total = {
        "jobs": len(jobs),
        "final": sum(job["status"] in FINAL for job in jobs),
        "refusals": categories,
        **summed,
        "tokens_per_byte_at_most": _per_byte(summed["window_tokens"], summed["window_bytes"]),
        "most_reads": max((job["most_reads"] for job in jobs), default=0),
        "largest_summary": None if largest is None else largest["largest_summary"],
        "largest_summary_thinking": None
        if largest is None
        else largest["largest_summary_thinking"],
        "summary_over": any(job["summary_over"] for job in jobs),
    }
    return {"jobs": jobs, "total": total}
