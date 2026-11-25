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

import time
from typing import Any

from . import rules
from .conftest import FULL_JOB, PER_TAKEOVER, SHORT_JOB, Running, Stack, failure, show
from .rules import SUMMARIES, TRIGGER, Seen

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

# The seed of the planned kills, the trigger of the workers that take a job
# over in the run with another trigger, and the tracker's first numbers in
# the run with other issue numbers: six digits, then seven.
SEED = 293
OTHER_TRIGGER_VALUE = 2800
SAME_WIDTH = 200_000
WIDER = 1_000_000

# The fixed text of each clause. A run with a mechanism switched off names
# one of the first six, so each is one string on one line.
WINDOW_REFUSAL = "the job's last request was refused for the context window on a scripted turn"
SUMMARY_STEPS = "the job does not have exactly N summary steps"
LACKING = "a request at a key the uninterrupted run lacks"
OPENING = "a window does not open with the summary text and the continuation line"
OTHER_TRIGGER = "a worker with another trigger sent other requests than the first worker would have"
OVERSIZE = "the window opened at the trigger and the job was not failed for it"
NAMED = (WINDOW_REFUSAL, SUMMARY_STEPS, LACKING, OPENING, OTHER_TRIGGER, OVERSIZE)
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
FAILPOINT_ONCE = "the failpoint did not stop a worker exactly once"
STOP_SUMMARY = "the first worker did not stop right after the job's first summary"
STOP_REQUEST = "the first worker did not stop at a summary request that the next worker sent"
NOT_CARRIED_ON = "the job was not taken over and carried on by another worker"
INCONCLUSIVE = "INCONCLUSIVE: a planned kill did not reach the job's holder inside its request"
FEW_TAKEOVERS = "the job has fewer takeovers than kills were planned for it"
BAD_SUMMARY = "a bad summary answer did not fail the job at once with nothing run after it"
SIZES = "a request's size is not the uninterrupted run's at its key"
LOW_NUMBER = "the tracker did not number this run's issues from its new first number"
SUMMARY_MOVED = "a summary request is not at the uninterrupted run's key"
NOT_MOVED = "a seventh digit in the issue numbers did not move the first summary a turn earlier"
WRONG_SETTING = "the workers that took the job over do not have the other trigger"
PINNED = "a job whose first request named no trigger did not go on as a job with no trigger"
PROBE = "the job with trigger 1 did not show the size of a window's first request"
TRACKER_BACK = "the tracker did not come back on its default numbers"


def clauses(job: Seen, baseline: Seen, *, unsent: int, exactly: bool = False) -> list[str]:
    """The lines of the clauses that every finished job of the suite is held to.

    `unsent` is the number of `model_request` steps whose request may be
    missing at the provider: none when nobody stopped a worker. With
    `exactly`, that many have to be missing.
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
    if (found := rules.unsent_over(matched, unsent, exactly=exactly)) is not None:
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


def ended(
    stack: Stack,
    run: Running,
    failures: list[str],
    checked: bool,
    *,
    fired: int = 0,
    job_ids: list[str] | None = None,
) -> None:
    """The end of every run, called from the test's own `finally`, before its one assert.

    The workers come back and no job is left running. Then the last clause:
    a worker stopped at a failpoint `fired` times. Then the clock and the record.
    """
    if not checked:
        # The record must not say `passed` for a run whose checks stopped half way.
        failures.append(failure(UNCHECKED, "an error ended the checks; see the traceback"))
    stack.settle(run, failures, job_ids)
    found = rules.failpoint_lines(stack.worker_log(run))
    if found != fired:
        text = FAILPOINT_ONCE if fired else FAILPOINT
        failures.append(failure(text, f"{found} lines in the workers' logs, expected {fired}"))
    stack.close(run, failures)


def tagged(lines: list[str], tag: str) -> list[str]:
    """The lines of one job among several: its name after the clause's fixed text."""
    return [line.replace(": ", f": {tag}: ", 1) for line in lines]


def counts(jobs: list[Seen], **more: Any) -> dict[str, Any]:
    """What a run's record holds about its jobs: numbers and statuses, never stored text."""
    return {
        "jobs": len(jobs),
        "statuses": [job.job["status"] for job in jobs],
        "seconds": [round(job.seconds, 1) for job in jobs],
        "summary_steps": [len(rules.of_kind(job.steps, "summary")) for job in jobs],
        "requests": [len(job.entries) for job in jobs],
        "takeovers": [job.job["takeovers"] for job in jobs],
        "questions": sum(len(job.questions) for job in jobs),
        "kills_planned": 0,
        "kills_reached": 0,
        "kill_latencies": [],
        **more,
    }


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
        ended(stack, run, failures, checked, job_ids=[baseline.id])
    show(f"{run.name}: {run.counts}")
    assert not failures, "\n".join(rules.ordered(failures, NAMED))


def test_a_job_stopped_after_its_first_summary_sends_what_the_uninterrupted_job_sent(
    stack: Stack, baseline: Seen
) -> None:
    """The workers stop at the failpoint `after_summary`: the first summary is stored, no more.

    The first worker's last step is the job's first `summary` step, and the
    failpoint line is in the workers' logs once. Another worker takes the
    job over, opens the window from that summary, and the job is held to
    every clause of the uninterrupted one: the same requests by key, three
    summaries, one issue for each call.
    """
    run = stack.begin("after-summary")
    failures: list[str] = []
    checked = False
    try:
        stack.workers_up(failpoint="after_summary")
        job = stack.run_job(within=FULL_JOB + PER_TAKEOVER)
        failures += clauses(job, baseline, unsent=job.job["takeovers"])
        if (found := rules.stopped_after_first_summary(job.steps)) is not None:
            failures.append(failure(STOP_SUMMARY, found))
        if (found := rules.carried_on(job.job, job.steps, summary=True)) is not None:
            failures.append(failure(NOT_CARRIED_ON, found))
        run.counts = counts([job])
        checked = True
    finally:
        ended(stack, run, failures, checked, fired=1)
    show(f"{run.name}: {run.counts}")
    assert not failures, "\n".join(rules.ordered(failures, NAMED))


def test_a_job_stopped_at_a_summary_request_sends_what_the_uninterrupted_job_sent(
    stack: Stack, baseline: Seen
) -> None:
    """The workers stop at `after_model_request`, limited to a summary request.

    The first worker's last step is a summary request that it never sent:
    exactly one `model_request` step has no request at the provider. The
    next request the provider got is that summary request, from the worker
    that took the job over. The failpoint line is in the logs once, and the
    job is held to every clause of the uninterrupted one.
    """
    run = stack.begin("summary-request")
    failures: list[str] = []
    checked = False
    try:
        stack.workers_up(failpoint="after_model_request", failpoint_summary=True)
        job = stack.run_job(within=FULL_JOB + PER_TAKEOVER)
        failures += clauses(job, baseline, unsent=1, exactly=True)
        if (found := rules.stopped_at_summary_request(job.steps, job.entries)) is not None:
            failures.append(failure(STOP_REQUEST, found))
        if (found := rules.carried_on(job.job, job.steps, summary=True)) is not None:
            failures.append(failure(NOT_CARRIED_ON, found))
        run.counts = counts([job])
        checked = True
    finally:
        ended(stack, run, failures, checked, fired=1)
    show(f"{run.name}: {run.counts}")
    assert not failures, "\n".join(rules.ordered(failures, NAMED))


def test_jobs_killed_at_planned_requests_send_what_the_uninterrupted_job_sent(
    stack: Stack, baseline: Seen
) -> None:
    """Five jobs, one at a time, each with one to three kills drawn from a fixed seed.

    A kill is planned for a request: after the job's k-th summary, while its
    m-th request step after that is open. The worker that holds the job is
    killed then and started again; the job waits out its lease and is taken
    over. Every job has a kill right after a summary, and one job a kill
    later in a window that a summary opened.

    Each job is held to every clause of the uninterrupted one, with the
    requests sent again compared too, and has at least as many takeovers as
    kills. A kill that came after its request, or never, shows nothing: the
    run is then INCONCLUSIVE. That is a finding. Keep the seed and the
    logs, and do not run again until it passes.
    """
    plan = rules.plan(SEED)
    assert not rules.plan_problems(plan), f"seed {SEED}: {rules.plan_problems(plan)}"
    show(f"seed {SEED}, planned kills (k, m) per job: {plan}")
    run = stack.begin("planned-kills")
    failures: list[str] = []
    checked = False
    jobs: list[Seen] = []
    kills: list[dict[str, Any]] = []
    try:
        stack.workers_up()
        for index, planned in enumerate(plan):
            pending = list(planned)
            done: list[dict[str, Any]] = []

            def each(
                job_id: str,
                steps: list[dict[str, Any]],
                index: int = index,
                pending: list[rules.Planned] = pending,
                done: list[dict[str, Any]] = done,
            ) -> None:
                if not pending or not steps:
                    return
                k, m = pending[0]
                target = rules.planned_seq(steps, k, m)
                if target is None:
                    return
                hit = None
                if steps[-1]["seq"] == target:
                    hit = stack.kill_holder(job_id, steps[-1])
                # Else the request already has a step after it: the kill would come late.
                pending.pop(0)
                done.append({"job": index, "k": k, "m": m, "seq": target, **(hit or {})})

            job = stack.run_job(within=FULL_JOB + PER_TAKEOVER * len(planned), each=each)
            jobs.append(job)
            failures += tagged(clauses(job, baseline, unsent=job.job["takeovers"]), f"job {index}")
            # A kill whose request never came is missed too.
            done += [{"job": index, "k": k, "m": m, "seq": None} for k, m in pending]
            for kill in done:
                kill["reached"] = "latency" in kill and rules.kill_reached(job.steps, kill["seq"])
            kills += done
            takeovers = job.job["takeovers"]
            if takeovers < len(planned):
                found = f"job {index}: {takeovers} takeovers, {len(planned)} kills planned"
                failures.append(failure(FEW_TAKEOVERS, found))
        missed = [(kill["job"], kill["k"], kill["m"]) for kill in kills if not kill["reached"]]
        if missed:
            found = f"seed {SEED}, (job, k, m) {missed}; keep the seed and the logs, do not rerun"
            failures.append(failure(INCONCLUSIVE, found))
        run.counts = counts(
            jobs,
            seed=SEED,
            kills_planned=len(kills),
            kills_reached=sum(kill["reached"] for kill in kills),
            kill_latencies=[kill.get("latency") for kill in kills],
            kills=[[kill["job"], kill["k"], kill["m"], kill["seq"]] for kill in kills],
            kills_missed=missed,
            inconclusive=bool(missed),
        )
        checked = True
    finally:
        ended(stack, run, failures, checked)
    show(f"{run.name}: {run.counts}")
    assert not failures, "\n".join(rules.ordered(failures, NAMED))


def test_a_bad_summary_answer_fails_the_job_and_nothing_runs_after_it(stack: Stack) -> None:
    """Six jobs, each on a scenario whose first summary gets one bad answer.

    The answers: a call that would open an issue, a refusal, empty text,
    text of white space only, a stop at `max_tokens`, and a stop at a stop
    sequence. Each time the summary is stored with its problem, the job
    ends `failed`, the provider got that one summary request, no tool is
    started after it, and the tracker holds no issue of the summary's call.
    """
    run = stack.begin("bad-summaries")
    failures: list[str] = []
    checked = False
    jobs: list[Seen] = []
    try:
        stack.workers_up()
        for fault in rules.FAULTS:
            job = stack.run_job(rules.bad_name(fault), within=SHORT_JOB)
            jobs.append(job)
            if not job.final:
                failures.append(failure(NOT_FINAL, f"{fault}: {job.job['status']}"))
            failures += [
                failure(BAD_SUMMARY, f"{fault}: {problem}")
                for problem in rules.bad_summary_problems(job, rules.bad_call_id(fault))
            ]
        run.counts = counts(jobs)
        checked = True
    finally:
        ended(stack, run, failures, checked)
    show(f"{run.name}: {run.counts}")
    assert not failures, "\n".join(rules.ordered(failures, NAMED))


def test_issue_numbers_of_the_same_width_move_no_summary_and_a_wider_number_does(
    stack: Stack, baseline: Seen
) -> None:
    """The same job with the tracker numbering from 200000, then from 1000000.

    Six digits again: every issue number is at least 200000, so the new
    tracker was in use; every answered request has the size of the
    uninterrupted job's at its key; every summary request is at the same
    key; and the job is held to every clause of the uninterrupted one.

    Seven digits: one response of the scenario is a token under the
    trigger, so the wider number takes it to the trigger and the first
    summary comes a turn earlier. That is the case in which the first part
    of this test would fail.
    """
    run = stack.begin("issue-numbers")
    failures: list[str] = []
    checked = False
    try:
        stack.workers_up()
        stack.tracker_up(str(SAME_WIDTH))
        same = stack.run_job()
        failures += clauses(same, baseline, unsent=0)
        numbers = sorted(number for found in same.issues.values() for number in found)
        low = rules.numbers_below(same.issues, SAME_WIDTH)
        if low or not numbers:
            failures.append(
                failure(LOW_NUMBER, f"{len(numbers)} issues, under {SAME_WIDTH}: {low}")
            )
        sizes = rules.estimate_differences(baseline.entries, same.entries)
        if sizes:
            failures.append(failure(SIZES, f"{len(sizes)} keys, the first {sizes[0]}"))
        keys, expected = rules.summary_keys(same.entries), rules.summary_keys(baseline.entries)
        if keys != expected:
            failures.append(failure(SUMMARY_MOVED, f"{keys}, the uninterrupted run {expected}"))
        stack.tracker_up(str(WIDER))
        wide = stack.run_job()
        if not wide.final:
            failures.append(failure(NOT_FINAL, f"seven digits: {wide.job['status']}"))
        first = rules.summary_keys(wide.entries)[:1]
        if not expected or first != [(expected[0][0], expected[0][1] - 1)]:
            failures.append(
                failure(NOT_MOVED, f"first summary at {first}, six digits {expected[:1]}")
            )
        run.counts = counts([same, wide], first_summary=[keys[:1], first])
        checked = True
    finally:
        try:
            stack.tracker_up()
        except RuntimeError as error:
            failures.append(failure(TRACKER_BACK, error))
        ended(stack, run, failures, checked)
    show(f"{run.name}: {run.counts}")
    assert not failures, "\n".join(rules.ordered(failures, NAMED))


def test_a_takeover_by_workers_with_another_trigger_sends_what_the_first_would_have(
    stack: Stack, baseline: Seen
) -> None:
    """Three jobs, each taken over by workers with another setting of the trigger.

    Every worker is killed, the job's lease runs out, and new workers start
    with the other setting, read back from each of them.

    - Job A starts with the trigger, is stopped at its first summary and
      taken over with a lower trigger, one that a later response of the
      scenario reaches a turn before the job's own trigger.
    - Job B is the same, taken over by workers with no trigger.
    - Job C starts on workers with no trigger and is stopped at the first
      request after a response that reaches the trigger. Workers with the
      trigger take it over.

    A and B send what the uninterrupted job sent, by key, and end
    `succeeded`. C never summarises: its requests are those of a job that
    ran with no trigger from start to end, and both end refused for the
    context window.
    """
    run = stack.begin("other-trigger")
    failures: list[str] = []
    checked = False
    jobs: list[Seen] = []
    try:
        for name, other in (("job A", OTHER_TRIGGER_VALUE), ("job B", None)):
            stack.workers_up()
            got: list[list[str]] = []

            def at_the_first_summary(
                job_id: str,
                steps: list[dict[str, Any]],
                other: int | None = other,
                got: list[list[str]] = got,
            ) -> None:
                if not got and rules.of_kind(steps, "summary"):
                    got.append(stack.take_over_with(job_id, trigger=other))

            job = stack.run_job(within=FULL_JOB + PER_TAKEOVER, each=at_the_first_summary)
            jobs.append(job)
            failures += tagged(clauses(job, baseline, unsent=job.job["takeovers"]), name)
            against = rules.compared(baseline, job)
            if not against.same:
                found = f"{name}, taken over with trigger {other}: {against.summary()}"
                failures.append(failure(OTHER_TRIGGER, found))
            wanted = "" if other is None else str(other)
            if not got or set(got[0]) != {wanted}:
                failures.append(failure(WRONG_SETTING, f"{name}: {got}, expected {wanted!r}"))
            if (carried := rules.carried_on(job.job, job.steps, summary=True)) is not None:
                failures.append(failure(NOT_CARRIED_ON, f"{name}: {carried}"))

        stack.workers_up(trigger=None)
        untriggered = stack.run_job(within=SHORT_JOB)
        back: list[list[str]] = []

        def at_the_trigger(job_id: str, steps: list[dict[str, Any]]) -> None:
            responses = rules.of_kind(steps, "model_response")
            if back or not responses or steps[-1]["kind"] != "model_request":
                return
            if rules.response_tokens(responses[-1]) >= TRIGGER:
                back.append(stack.take_over_with(job_id, trigger=TRIGGER))

        pinned = stack.run_job(within=SHORT_JOB + PER_TAKEOVER, each=at_the_trigger)
        jobs += [untriggered, pinned]
        for name, job in (("the job with no trigger", untriggered), ("job C", pinned)):
            how = rules.how_it_failed(job.job, job.entries, stack.window, stack.turns)
            if how is None or not how[0]:
                counted = f"status {job.job['status']}" if how is None else how[1]
                failures.append(failure(PINNED, f"{name} did not end at the window: {counted}"))
            if (summaries := rules.summarised(job)) is not None:
                failures.append(failure(PINNED, f"{name}: {summaries}"))
        against = rules.compared(untriggered, pinned)
        if not against.same:
            found = f"job C, taken over with trigger {TRIGGER}: {against.summary()}"
            failures.append(failure(OTHER_TRIGGER, found))
        if not back or set(back[0]) != {str(TRIGGER)}:
            failures.append(failure(WRONG_SETTING, f"job C: {back}, expected {str(TRIGGER)!r}"))
        if (carried := rules.carried_on(pinned.job, pinned.steps, summary=False)) is not None:
            failures.append(failure(NOT_CARRIED_ON, f"job C: {carried}"))
        run.counts = counts(jobs, triggers_after_takeover=[*got[:1], *back[:1]])
        checked = True
    finally:
        ended(stack, run, failures, checked)
    show(f"{run.name}: {run.counts}")
    assert not failures, "\n".join(rules.ordered(failures, NAMED))


class _TooLong(Exception):
    """The job that measures a window's first request went on long after it had."""


def test_a_window_that_opens_at_the_trigger_fails_the_job_for_it(stack: Stack) -> None:
    """A summary that leaves the next window at the trigger would be asked for again at once.

    A first job, with trigger 1, shows the size X of the first request
    after a summary: the provider's count of its input. It fails for being
    over its trigger. The job of this run then has the trigger X. Its first
    window after a summary opens at exactly the trigger, and it ends
    `failed` with the error that names X twice, with one `summary` step and
    no second summary request.
    """
    run = stack.begin("oversize")
    failures: list[str] = []
    checked = False
    jobs: list[Seen] = []
    try:
        stack.workers_up(trigger=1)
        read: dict[str, float] = {}

        def until_the_size_is_read(job_id: str, steps: list[dict[str, Any]]) -> None:
            if "size" not in read:
                size = rules.opening_input(stack.entries(job_id))
                if size is not None:
                    read["size"], read["at"] = size, time.monotonic()
            elif time.monotonic() - read["at"] > 30:
                raise _TooLong

        try:
            probe = stack.run_job(within=SHORT_JOB, each=until_the_size_is_read)
        except _TooLong:
            # With the check of the window's size switched off it would run to its end.
            probe = stack.seen(stack.submitted[-1])
            failures.append(failure(PROBE, f"not final 30 s after its size was read: {read}"))
        jobs.append(probe)
        size = rules.opening_input(probe.entries)
        if size is None:
            failures.append(failure(PROBE, f"no request after a summary request: {probe.job}"))
        else:
            if probe.job["error"] != rules.oversize_error(size, 1):
                found = f"status {probe.job['status']}, size {size}, not the error for trigger 1"
                failures.append(failure(PROBE, found))
            stack.workers_up(trigger=size)
            job = stack.run_job()
            jobs.append(job)
            if not job.final:
                failures.append(
                    failure(NOT_FINAL, f"{job.job['status']} after {job.seconds:.0f} s")
                )
            failures += [
                failure(OVERSIZE, f"trigger {size}: {problem}")
                for problem in rules.oversize_problems(job, size, size)
            ]
        run.counts = counts(jobs, size=size)
        checked = True
    finally:
        ended(stack, run, failures, checked)
    show(f"{run.name}: {run.counts}")
    assert not failures, "\n".join(rules.ordered(failures, NAMED))
