"""One job longer than the fake provider's context window, on the long stack.

The job reads the notes of 43 parts and opens four issues. Its scenario has
a context window, and with the stack's trigger the job stores three
summaries and every request fits the window. The fake provider's log is the
record of what the workers really sent, and the tracker's own list of what
happened outside the runtime.

Each test is one run. It checks every clause that applies, also after one
has failed, and ends with one assert over the lines of the clauses that
failed. A line is the clause's fixed text, then what was found. The checks
themselves are in `rules.py`.
"""

from . import rules
from .conftest import Stack, failure, show
from .rules import SUMMARIES, Seen

# The suite's own copies of the worker's two texts and of the scenario's
# summary. A change to any of them in the code has to fail here.
INSTRUCTION = (
    "Your earlier messages are about to be removed to make room. Write a summary that"
    " lets you continue this task with nothing else: the task and every instruction you"
    " were given, the facts you found that later steps need, what you have done, and"
    " what is left to do. Plain text. Do not call a tool."
)
CONTINUATION = (
    "That was your own summary of this job so far, written before your earlier messages"
    " were removed. Continue the task from where it stops."
)
SUMMARY_TEXT = (
    "The task: read the notes of every part in order, open an issue where they ask for one,"
    " then say done. Read so far: every part before the turn named below."
)

# The fixed text of each clause. A run with a mechanism switched off names
# one of the first four, so each is one string on one line.
WINDOW_REFUSAL = "the job's last request was refused for the context window on a scripted turn"
SUMMARY_STEPS = "the job does not have exactly N summary steps"
LACKING = "a request at a key the uninterrupted run lacks"
OPENING = "a window does not open with the summary text and the continuation line"
NAMED = (WINDOW_REFUSAL, SUMMARY_STEPS, LACKING, OPENING)
NOT_SUCCEEDED = "the job did not end succeeded"
NOT_FINAL = "the job was not final in time"
OTHER_FAILURE = "the job failed, and not at the context window on a scripted turn"
SUMMARY_WINDOWS = "the provider's log does not hold N windows with an answered summary request"
SUMMARY_REQUEST = "a summary request is not the request before it plus the instruction"
DIFFERS = "a request is not the uninterrupted run's request at its key"
MISSING = "a request of the uninterrupted run was never sent"
ORDER = "a request's key comes before the key of the request before it"
NUMBERS = "an issue number in a request is not the tracker's number for that call"
ISSUES = "the tracker does not hold one issue for each scripted call"
LOG_FULL = "the provider's log reached its cap and may have dropped requests"
NO_STEP = "the provider got a request that no model_request step announced"
UNSENT = "a model_request step's request never reached the provider"
TOOL_NAMES = "a model_request step does not name the tools its request offered"
QUESTION = "the job asked a person a question"
TAKEN_OVER = "the uninterrupted job was taken over"
REPEATED = "the uninterrupted job sent a request twice"
FAILPOINT = "a worker stopped at a failpoint in a run that switches none on"
UNCHECKED = "the run's checks did not all run"


def clauses(job: Seen, baseline: Seen, *, unsent: int) -> list[str]:
    """The lines of the clauses that every finished job of the suite is held to.

    `unsent` is the number of `model_request` steps whose request may be
    missing at the provider: none when nobody stopped a worker.
    """
    lines = []
    if not job.final:
        counts = f"{job.job['status']} after {job.seconds:.0f} s, {len(job.steps)} steps"
        lines.append(failure(NOT_FINAL, counts))
    if (found := rules.not_succeeded(job.job)) is not None:
        lines.append(failure(NOT_SUCCEEDED, found))
    if (found := rules.summary_step_count(job.steps, SUMMARIES)) is not None:
        lines.append(failure(SUMMARY_STEPS, found))
    if (found := rules.summary_window_count(job.entries, SUMMARIES)) is not None:
        lines.append(failure(SUMMARY_WINDOWS, found))
    lines += [
        failure(SUMMARY_REQUEST, problem)
        for problem in rules.summary_request_problems(job.steps, job.entries, INSTRUCTION)
    ]
    against = rules.compared(baseline, job)
    for text, keys in (
        (LACKING, against.lacking),
        (DIFFERS, against.differs),
        (MISSING, against.missing),
        (ORDER, against.out_of_order),
    ):
        if keys:
            lines.append(failure(text, f"{len(keys)} keys, the first {keys[0]}"))
    lines += [failure(NUMBERS, number) for number in against.numbers]
    lines += [
        failure(OPENING, problem)
        for problem in rules.opening_problems(job.entries, SUMMARY_TEXT, CONTINUATION)
    ]
    # Stricter than the tracker clause, which allows an approved rerun: on
    # the fake provider no call of this job should need a person.
    lines += [
        failure(QUESTION, f"question {question['question']}: {question['reason']}")
        for question in job.questions
    ]
    approved = rules.approvals(job.questions)
    lines += [
        failure(ISSUES, problem)
        for problem in rules.issue_problems(job.id, baseline.steps, job.issues, approved)
    ]
    if (found := rules.log_full(job.log_length)) is not None:
        lines.append(failure(LOG_FULL, found))
    matched = rules.matched(job.entries, job.steps)
    if matched.without_step:
        count, first = len(matched.without_step), matched.without_step[0]
        lines.append(failure(NO_STEP, f"{count} requests, the first the provider's {first}"))
    if (found := rules.unsent_over(matched, unsent)) is not None:
        lines.append(failure(UNSENT, found))
    lines += [failure(TOOL_NAMES, problem) for problem in rules.tool_name_problems(matched.pairs)]
    return lines


def uninterrupted(job: Seen, window: int, turns: int) -> list[str]:
    """The lines of the clauses that only the job nobody interrupted is held to."""
    lines = []
    how = rules.how_it_failed(job.job, job.entries, window, turns)
    if how is not None:
        at_the_window, counts = how
        lines.append(failure(WINDOW_REFUSAL if at_the_window else OTHER_FAILURE, counts))
    if (found := rules.taken_over(job.job)) is not None:
        lines.append(failure(TAKEN_OVER, found))
    repeated = rules.repeated_keys(job.entries)
    if repeated:
        lines.append(failure(REPEATED, f"{len(repeated)} keys, the first {repeated[0]}"))
    return lines


def test_a_job_longer_than_the_window_finishes_through_three_summaries(
    stack: Stack, baseline: Seen
) -> None:
    """The job with nobody interrupting it. Every other run is compared with this one.

    - It ends `succeeded` with three `summary` steps, one in each of the
      first three windows, and each summary request is the request before
      it, that request's answer and its results, and the fixed instruction.
    - Each later window opens with the summary as the provider sent it,
      turn marker included, and the continuation line.
    - In the provider's order no request's key goes back, and each issue
      number in a request is the tracker's number for that call.
    - The tracker holds one issue for each call the script makes.
    - Every request the provider got was announced by a `model_request`
      step, every such step's request arrived, and each step names the
      tools its request offered.
    - No worker stopped at a failpoint, nobody took the job over, and it
      asked no question.

    With the trigger switched off, the job fails instead: its last request
    is refused for the context window, at a turn the script still holds.
    """
    run = stack.begin("uninterrupted", of=baseline)
    failures: list[str] = []
    checked = False
    try:
        failures += clauses(baseline, baseline, unsent=0)
        failures += uninterrupted(baseline, stack.window, stack.turns)
        run.counts = {
            "jobs": 1,
            "status": baseline.job["status"],
            "seconds": round(baseline.seconds, 1),
            "summary_steps": len(rules.of_kind(baseline.steps, "summary")),
            "summary_keys": rules.summary_keys(baseline.entries),
            "requests": len(baseline.entries),
            "steps": len(baseline.steps),
            "tokens_used": baseline.job["tokens_used"],
            "takeovers": baseline.job["takeovers"],
            "kills_planned": 0,
            "kills_reached": 0,
            "kill_latencies": [],
            "questions": len(baseline.questions),
            "issues": sum(len(numbers) for numbers in baseline.issues.values()),
        }
        checked = True
    finally:
        if not checked:
            # The record must not say `passed` for a run whose checks stopped half way.
            failures.append(failure(UNCHECKED, "an error ended the checks; see the traceback"))
        stack.settle(run, failures, [baseline.id])
        fired = rules.failpoint_lines(stack.worker_log(run))
        if fired:
            failures.append(failure(FAILPOINT, f"{fired} lines in the workers' logs"))
        stack.close(run, failures)
    show(f"{run.name}: {run.counts}")
    assert not failures, "\n".join(rules.ordered(failures, NAMED))
