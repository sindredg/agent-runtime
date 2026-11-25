"""The pass line of the long-job suite, and what it starts the stack with.

One job is longer than the fake provider's context window and goes on
through summaries. The suite compares what the fake provider received, the
job's step log and the tracker's own list of issues. Each check is a
function here over those three, so the unit tests can give it a log that
passes and one that breaks only that check.

Nothing in this module talks to the stack.
"""

import copy
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from fake_provider.requests import turn_index
from fake_provider.summary import place

# The scenario of the job, and the trigger it is sized for: with it the job
# stores three summaries and every request fits the window.
MODEL = "long-window"
TRIGGER = 3000
SUMMARIES = 3
WORKERS = 2
# The lease of the long stack, in seconds: the default.
LEASE = 30.0
# Seconds the fake waits inside every answer on the stack. A request then
# stays open long enough to be seen as the job's last step and to stop its
# worker inside it. The packaged scenario has no wait.
DELAY = 2.0
# The bad answers a summary request can get, each on a scenario of its own.
FAULTS = ("tool_use", "refusal", "empty", "whitespace", "max_tokens", "stop_sequence")
# What the long stack reads from the shell. The suite sets every one on every
# compose call, so none comes from the shell or from `.env`.
SETTINGS = (
    "LONG_SCENARIO_DIR",
    "SUMMARY_TRIGGER",
    "FAILPOINT",
    "FAILPOINT_SUMMARY",
    "FAKE_TRACKER_FIRST_ISSUE",
)
PROMPT = (
    "Read the notes of every part in order, open an issue where they ask for one,"
    " then say you are done."
)
NOT_FINAL = ("queued", "running", "waiting", "needs_approval")
# What a worker logs when it stops at a failpoint.
FIRED = "failpoint reached, stopping the worker"
# The fake provider keeps its newest 10,000 requests and drops older ones
# without a word. A log that reached the cap may have lost some.
LOG_CAP = 10_000
# The provider's words for a request over the context window.
TOO_LONG = re.compile(r"prompt is too long: (\d+) tokens > (\d+) maximum")
CREATED = re.compile(r"created issue (\d+):")
MASKED = "created issue N:"
NONE = {"type": "none"}
# pytest keeps whole lines of a failure's message while they fit in this many
# characters, and the message starts with the exception's name.
MESSAGE_BUDGET = 500
MESSAGE_PREFIX = len("AssertionError: ")
# The most characters of a line that is moved to the top of the message.
NAMED_WIDTH = 150

type Key = tuple[int, int]


# What the stack is started with


def compose_env(
    environ: Mapping[str, str],
    *,
    scenario_dir: str,
    trigger: int | None = None,
    failpoint: str = "",
    failpoint_summary: bool = False,
    first_issue: str = "",
) -> dict[str, str]:
    """The environment of one compose call: `environ` with every setting of the suite set.

    A setting the caller leaves out is the empty string, never the value
    `environ` holds: the stack file then takes its default.
    """
    return {
        **environ,
        "LONG_SCENARIO_DIR": scenario_dir,
        "SUMMARY_TRIGGER": "" if trigger is None else str(trigger),
        "FAILPOINT": failpoint,
        "FAILPOINT_SUMMARY": "1" if failpoint_summary else "",
        "FAKE_TRACKER_FIRST_ISSUE": first_issue,
    }


def bad_name(fault: str) -> str:
    """The scenario whose first summary gets `fault`. A scenario's name takes no underscore."""
    return f"{MODEL}-bad-{fault.replace('_', '-')}"


def scenario_files(packaged: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The scenario files of a session, by name, made from the packaged scenario.

    The job's scenario keeps its name and gets the wait on every turn and on
    its summary. A wait changes no request, so the sizes are the packaged
    ones. Each bad-summary scenario is the packaged one, without a wait,
    whose first summary gets one bad answer.
    """
    slow = copy.deepcopy(dict(packaged))
    for turn in slow["turns"]:
        turn["delay_seconds"] = DELAY
    slow["summary"]["delay_seconds"] = DELAY
    files = {MODEL: slow}
    for fault in FAULTS:
        bad = copy.deepcopy(dict(packaged))
        bad["summary"]["bad"] = {"1": fault}
        files[bad_name(fault)] = bad
    return files


def job_files(packaged: Mapping[str, Any]) -> dict[str, str]:
    """The files of the job's spec: one short line for each file the scenario reads."""
    paths = [
        str(block["input"]["path"])
        for turn in packaged["turns"]
        for block in turn["content"]
        if block.get("type") == "tool_use" and block.get("name") == "read_file"
    ]
    return {path: f"{path}: read.\n" for path in paths}


# What was read about a job


@dataclass
class Seen:
    """One job, as the API, the fake provider and the tracker show it."""

    # The API's view of the job when the wait for it ended.
    job: dict[str, Any]
    steps: list[dict[str, Any]]
    # The fake provider's entries that carry the job's id, in its order.
    entries: list[dict[str, Any]]
    # The tracker's numbers by operation id, for this job's operations, in its order.
    issues: dict[str, list[int]]
    # Every question the suite approved: its step, operation, tool and reason.
    questions: list[dict[str, Any]] = field(default_factory=list)
    # How many entries the fake's whole log held when the job was read.
    log_length: int = 0
    started: datetime | None = None
    # Seconds from the submission to the job's final status, or to the end of the wait.
    seconds: float = 0.0
    # Seconds the wall clock moved against the monotonic clock meanwhile.
    drift: float = 0.0

    @property
    def id(self) -> str:
        return str(self.job["id"])

    @property
    def final(self) -> bool:
        return self.job["status"] not in NOT_FINAL


def of_kind(steps: Sequence[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [step for step in steps if step["kind"] == kind]


def issues_of(job_id: str, tracker: Sequence[dict[str, Any]]) -> dict[str, list[int]]:
    """The tracker's numbers for the job's operations, by operation id."""
    found: dict[str, list[int]] = {}
    for issue in tracker:
        if issue["operation_id"].startswith(f"{job_id}:"):
            found.setdefault(issue["operation_id"], []).append(issue["id"])
    return found


# The key of a request


def key(body: Mapping[str, Any]) -> Key:
    """(window, turn) of a request, read from its body as the fake reads it.

    The window is the number of summaries the first message's marker names,
    and the turn is the marker's turn plus the assistant messages the
    request holds. Not the entry's `turn`: a refused entry has none. A
    summary request in window K has the key of the turn it replaces, and
    the first request of window K + 1 has that turn too.
    """
    messages = body["messages"]
    summaries, start = place(messages)
    return summaries, start + turn_index(messages)


def is_summary(entry: Mapping[str, Any]) -> bool:
    return entry["body"].get("tool_choice") is not None


def by_key(entries: Sequence[dict[str, Any]]) -> dict[tuple[Key, bool], dict[str, Any]]:
    """The entries by key and by whether they ask for a summary. The last one at a key counts."""
    return {(key(entry["body"]), is_summary(entry)): entry for entry in entries}


def summary_keys(entries: Sequence[dict[str, Any]]) -> list[Key]:
    """The key of each summary request, in the fake's order."""
    return [key(entry["body"]) for entry in entries if is_summary(entry)]


def repeated_keys(entries: Sequence[dict[str, Any]]) -> list[Key]:
    """Keys at which the job sent a request more than once. None when nobody interrupts it."""
    seen: set[tuple[Key, bool]] = set()
    repeated = []
    for entry in entries:
        place_of = (key(entry["body"]), is_summary(entry))
        if place_of in seen:
            repeated.append(place_of[0])
        seen.add(place_of)
    return repeated


# The checks. Each returns what is wrong, and nothing when nothing is.


def not_succeeded(job: Mapping[str, Any]) -> str | None:
    """The job's status and error unless it ended `succeeded`."""
    if job["status"] == "succeeded":
        return None
    return f"status {job['status']}, error {str(job.get('error'))[:200]!r}"


def summary_step_count(steps: Sequence[dict[str, Any]], expected: int) -> str | None:
    """The number of `summary` steps unless it is `expected`."""
    found = len(of_kind(steps, "summary"))
    return None if found == expected else f"{found} summary steps, expected {expected}"


def summary_window_count(entries: Sequence[dict[str, Any]], expected: int) -> str | None:
    """The windows with an answered summary request unless they are windows 0 to `expected` - 1."""
    answered = sorted(
        {key(e["body"])[0] for e in entries if is_summary(e) and e.get("status") == 200}
    )
    if answered == list(range(expected)):
        return None
    return f"windows with an answered summary request {answered}, expected {expected} from 0"


def summary_request_problems(
    steps: Sequence[dict[str, Any]], entries: Sequence[dict[str, Any]], instruction: str
) -> list[str]:
    """Each summary request that is not the request before it plus the instruction.

    The request before it is the job's ordinary request one turn earlier in
    the same window, found by key. The summary request has its `system` and
    `tools`, and its messages, then the answer that request got, then one
    user message: a result for each of the answer's tool calls, in call
    order, and the instruction as the last block. Its `tool_choice` is none.
    """
    responses = {
        step["payload"]["id"]: step["payload"] for step in of_kind(steps, "model_response")
    }
    ordinary = {
        key(entry["body"]): entry
        for entry in entries
        if not is_summary(entry) and entry.get("response_id") is not None
    }
    problems = []
    for entry in entries:
        if not is_summary(entry):
            continue
        body = entry["body"]
        window, turn = at = key(body)
        if body["tool_choice"] != NONE:
            problems.append(f"{at}: tool_choice is {body['tool_choice']}")
        before = ordinary.get((window, turn - 1))
        if before is None:
            problems.append(f"{at}: the job has no answered request at {(window, turn - 1)}")
            continue
        answer = responses.get(before["response_id"])
        if answer is None:
            problems.append(f"{at}: no model_response step has the id {before['response_id']}")
            continue
        for part in ("system", "tools"):
            if body.get(part) != before["body"].get(part):
                problems.append(f"{at}: its {part} is not that of the request before it")
        calls = [block["id"] for block in answer["content"] if block.get("type") == "tool_use"]
        messages = body["messages"]
        earlier, last = messages[:-1], messages[-1]
        expected = [
            *before["body"]["messages"],
            {"role": "assistant", "content": answer["content"]},
        ]
        blocks = last["content"] if isinstance(last.get("content"), list) else []
        results = [
            block.get("tool_use_id") for block in blocks[:-1] if block.get("type") == "tool_result"
        ]
        if earlier != expected:
            problems.append(f"{at}: its messages are not the request before it and its answer")
        elif last.get("role") != "user" or len(blocks) != len(calls) + 1 or results != calls:
            problems.append(f"{at}: its last message does not hold one result for each call")
        elif blocks[-1] != {"type": "text", "text": instruction}:
            problems.append(f"{at}: its last block is not the instruction")
    return problems


def opening_problems(
    entries: Sequence[dict[str, Any]], summary_text: str, continuation: str
) -> list[str]:
    """Each window that does not open with the summary as the fake sent it and the continuation.

    The fake numbers a summary one more than the window it ends, and marks it
    with the turn of its request. Both come from the summary request, never
    from the request that opens the window: that one's key is read from the
    marker this check is about. Each answered summary has to be followed by
    such a request.
    """
    problems = []
    # The message the window that is open starts with: None in the first one.
    expected: dict[str, Any] | None = None
    # The key of an answered summary request that no window has opened from yet.
    owed: Key | None = None
    for entry in entries:
        body = entry["body"]
        if is_summary(entry):
            if entry.get("status") == 200:
                window, turn = owed = key(body)
                text = f"{summary_text}\n\n[summary {window + 1}] [turn {turn}]\n\n{continuation}"
                expected = {"role": "user", "content": [{"type": "text", "text": text}]}
            continue
        held = len(body["messages"])
        if owed is not None and held != 1:
            problems.append(f"after the summary at {owed}: the next request holds {held} messages")
        elif expected is not None and held == 1 and body["messages"] != [expected]:
            problems.append(
                f"after the summary at {owed or key(body)}: the opening message differs"
            )
        owed = None
    if owed is not None:
        problems.append(f"after the summary at {owed}: no request follows")
    return problems


def issue_problems(
    job_id: str,
    reference_steps: Sequence[dict[str, Any]],
    issues: Mapping[str, Sequence[int]],
    approvals: Mapping[str, int],
) -> list[str]:
    """Where the tracker's list for the job is not one issue per scripted call.

    The calls are the `create_issue` calls in the answers of the job nobody
    interrupted. Each has one issue, and one more for each rerun a person
    approved. An operation the script does not hold is wrong too.
    """
    calls = [
        block["id"]
        for step in of_kind(reference_steps, "model_response")
        for block in step["payload"]["content"]
        if block.get("type") == "tool_use" and block.get("name") == "create_issue"
    ]
    expected = {f"{job_id}:{call}": 1 for call in calls}
    problems = []
    for operation in expected:
        allowed = 1 + approvals.get(operation, 0)
        found = len(issues.get(operation, ()))
        if found != allowed:
            problems.append(f"{operation}: {found} issues, expected {allowed}")
    problems += [
        f"{operation}: {len(numbers)} issues for a call the script does not make"
        for operation, numbers in issues.items()
        if operation not in expected
    ]
    return problems


@dataclass
class Matched:
    """The fake's entries of a job against its `model_request` steps."""

    # A step and the entry of the request it announced.
    pairs: list[tuple[dict[str, Any], dict[str, Any]]]
    # The fake's `seq` of each entry that no step announced.
    without_step: list[int]
    # Steps whose request never reached the fake: written, and the worker stopped.
    unsent: int


def matched(entries: Sequence[dict[str, Any]], steps: Sequence[dict[str, Any]]) -> Matched:
    """Walk the entries in order along the `model_request` steps in order.

    An entry belongs to the next step with its `max_tokens` and its number
    of messages. Steps passed over on the way were never sent.
    """
    requests = of_kind(steps, "model_request")
    pairs, without_step, pointer = [], [], 0
    for entry in entries:
        body = entry["body"]
        found = next(
            (
                index
                for index in range(pointer, len(requests))
                if requests[index]["payload"].get("max_tokens") == body.get("max_tokens")
                and requests[index]["payload"].get("messages") == len(body["messages"])
            ),
            None,
        )
        if found is None:
            without_step.append(entry["seq"])
            continue
        pairs.append((requests[found], entry))
        pointer = found + 1
    return Matched(pairs, without_step, len(requests) - len(pairs))


def unsent_over(found: Matched, allowed: int) -> str | None:
    """The steps whose request never arrived, when they are more than `allowed`.

    None is allowed when nobody stopped a worker. A worker that is stopped
    between the step and the call leaves one.
    """
    if found.unsent <= allowed:
        return None
    return f"{found.unsent} steps, at most {allowed} expected"


def log_full(length: int) -> str | None:
    """The length of the provider's whole log, when it reached the cap."""
    return f"{length} entries, the cap is {LOG_CAP}" if length >= LOG_CAP else None


def taken_over(job: Mapping[str, Any]) -> str | None:
    """The job's takeovers, when a job that nobody interrupted has any."""
    return f"{job['takeovers']} takeovers" if job["takeovers"] else None


def approvals(questions: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """How many reruns a person approved, by operation id."""
    counted: dict[str, int] = {}
    for question in questions:
        operation = str(question["operation_id"])
        counted[operation] = counted.get(operation, 0) + 1
    return counted


def tool_name_problems(pairs: Sequence[tuple[dict[str, Any], dict[str, Any]]]) -> list[str]:
    """Each `model_request` step whose tool names are not those of its request at the fake."""
    problems = []
    for step, entry in pairs:
        offered = [tool["name"] for tool in entry["body"].get("tools") or []]
        if step["payload"].get("tools") != offered:
            problems.append(f"step {step['seq']}: {step['payload'].get('tools')}, sent {offered}")
    return problems


def failpoint_lines(log: str) -> int:
    """How often a worker logged that it stopped at a failpoint."""
    return sum(FIRED in line for line in log.splitlines())


def how_it_failed(
    job: Mapping[str, Any], entries: Sequence[dict[str, Any]], window: int, turns: int
) -> tuple[bool, str] | None:
    """How a failed job ended at the fake: (refused for the window, the counts). None if not failed.

    Refused for the window means the job's last request got a 400 with the
    provider's words for a prompt that is too long, at an estimate over the
    window, on a turn the script holds. A 400 past the end of the script,
    or a job stopped by its budget, is another failure.
    """
    if job["status"] != "failed":
        return None
    if not entries:
        return False, f"the fake got no request; error {str(job.get('error'))[:200]!r}"
    last = entries[-1]
    turn = key(last["body"])[1]
    estimate = last.get("input_estimate")
    counts = (
        f"last request: status {last.get('status')}, estimate {estimate}, window {window},"
        f" turn {turn} of {turns}; error {str(job.get('error'))[:200]!r}"
    )
    refused = (
        last.get("status") == 400
        and TOO_LONG.fullmatch(str(last.get("error"))) is not None
        and isinstance(estimate, int)
        and estimate > window
        and turn < turns
    )
    return refused, counts


@dataclass
class Compared:
    """A job's requests against those of the job nobody interrupted, by key."""

    # Keys at which the request is not the uninterrupted job's.
    differs: list[Key] = field(default_factory=list)
    # Keys the uninterrupted job has no request at.
    lacking: list[Key] = field(default_factory=list)
    # Keys of the uninterrupted job that this job never sent.
    missing: list[Key] = field(default_factory=list)
    # Keys that come before the key of the request before them.
    out_of_order: list[Key] = field(default_factory=list)
    # Issue numbers in a request that are not the tracker's for that call.
    numbers: list[str] = field(default_factory=list)

    @property
    def same(self) -> bool:
        return not (
            self.differs or self.lacking or self.missing or self.out_of_order or self.numbers
        )


def masked(
    body: Mapping[str, Any], job_id: str, issues: Mapping[str, Sequence[int]]
) -> tuple[dict[str, Any], list[str]]:
    """What two jobs' requests are compared on, with each new issue's number masked.

    The parts are `system`, `tools`, `tool_choice` and `messages`. Only the
    number in `created issue N:` is masked, and only in a result that
    answers a `create_issue` call. That number has to be the last one the
    tracker lists for the call's operation; the second value says where it
    is not.
    """
    part = copy.deepcopy({name: body.get(name) for name in ("system", "tools", "tool_choice")})
    messages = copy.deepcopy(body["messages"])
    creates: set[str] = set()
    wrong = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "tool_use" and block.get("name") == "create_issue":
                creates.add(block["id"])
            if block.get("type") != "tool_result" or block.get("tool_use_id") not in creates:
                continue
            found = CREATED.match(block["content"]) if isinstance(block["content"], str) else None
            if found is None:
                continue
            operation = f"{job_id}:{block['tool_use_id']}"
            listed = issues.get(operation, ())
            if not listed or int(found[1]) != listed[-1]:
                wrong.append(f"{operation}: {found[1]} in the request, the tracker lists {listed}")
            block["content"] = MASKED + block["content"][found.end() :]
    return {**part, "messages": messages}, wrong


def compared(reference: Seen, job: Seen) -> Compared:
    """Every request the fake got for `job`, resends included, against `reference` by key."""
    theirs = {
        place_of: masked(entry["body"], reference.id, reference.issues)[0]
        for place_of, entry in by_key(reference.entries).items()
    }
    result = Compared()
    seen: set[tuple[Key, bool]] = set()
    before: Key | None = None
    for entry in job.entries:
        at = key(entry["body"])
        place_of = (at, is_summary(entry))
        ours, wrong = masked(entry["body"], job.id, job.issues)
        result.numbers += [number for number in wrong if number not in result.numbers]
        if before is not None and at < before:
            result.out_of_order.append(at)
        before = at
        seen.add(place_of)
        if place_of not in theirs:
            if (at, not place_of[1]) in theirs:
                # The other kind of request at that key: a summary where the
                # uninterrupted job went on, or the reverse.
                result.differs.append(at)
            else:
                result.lacking.append(at)
        elif ours != theirs[place_of]:
            result.differs.append(at)
    result.missing = sorted(place_of[0] for place_of in theirs.keys() - seen)
    return result


# The message of a failed test


def ordered(lines: Sequence[str], named: Sequence[str]) -> list[str]:
    """The failure lines, with one line for each of the `named` texts first.

    A run with a mechanism switched off is judged by the text of the check
    it names, and only the start of a failure's message is kept. So the
    first line that starts with each named text goes to the top, cut short:
    to 150 characters, and to less when that many lines would not fit in
    what is kept. Every other line follows as it was.
    """
    first = {}
    for text in named:
        found = next((index for index, line in enumerate(lines) if line.startswith(text)), None)
        if found is not None:
            first[found] = text
    if not first:
        return list(lines)
    room = (MESSAGE_BUDGET - MESSAGE_PREFIX) // len(first) - 1
    width = min(NAMED_WIDTH, room)
    # Never cut inside the named text itself.
    top = [lines[index][: max(width, len(text))] for index, text in first.items()]
    return top + [line for index, line in enumerate(lines) if index not in first]
