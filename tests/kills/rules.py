"""The pass line of the kill runs, and the settings a run may start with.

The chaos tests compare an interrupted job with a job nobody interrupted,
request by request. A real model does not give the same answer twice, so
that comparison does not exist here. These rules judge a job by what is
left when it is over: its status, the tracker's own list of issues, and its
token count.

Nothing in this module talks to the stack. The unit tests run it on
made-up jobs.
"""

import random
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

# Each mode's name, and the provider URL every worker must then have.
PROVIDERS = {
    "fake": "http://fake-provider:8000",
    "claude": "https://api.anthropic.com",
}
# The fake provider's scenario for the job: every answer half a second late.
FAKE_MODEL = "ci-triage-slow"
# What every job may spend. The API's default budget is twenty times this.
TOKEN_BUDGET = 50_000
# Jobs one invocation may submit to the Claude API. The full plan is 33.
# KILLS_JOB_LIMIT sets a lower number, and a limit on the fake provider.
API_JOB_LIMIT = 40
# Jobs that run first with nobody interrupting them. The longest one sets
# the window the kill times are drawn from.
UNINTERRUPTED = 3
# The failpoints the kill is aimed with, both on the call that opens the issue.
FAILPOINTS = ("after_tool_intent", "after_tool_ran")
TOOL = "create_issue"
# The longest window a run may be given, in seconds. A job has five minutes on the API.
MAX_WINDOW = 300.0
# The block type a model's reasoning comes back in. A later request sends it
# back unchanged. The API's documentation says Claude Fable 5.2 checks such a
# block; no run here has shown it refuse one.
THINKING = "thinking"
# What the error of a job holds when the API refused a `thinking` block whose
# `signature` it could not read: the worker's words for the status, and the
# API's two names.
REFUSED = ("provider answered 400", "signature", "thinking")
# The API adds this when the signature is intact and the conversation before
# the block is not the one that produced it. That is another refusal.
OTHER_CONVERSATION = "bound to a different conversation"
# Characters of a failed job's error that a results line keeps.
ERROR_LENGTH = 500
# A step that moves a held job to one of these is written at the lease epoch
# it leaves. The claim after it is not a takeover.
NOT_A_TAKEOVER = ("waiting", "needs_approval")


@dataclass(frozen=True)
class Settings:
    provider: str
    model: str
    runs: int
    failpoint_runs: int
    seed: str
    # Seconds the kill times are drawn from, to repeat a recorded run. None: measured.
    window: float | None = None
    # Jobs the invocation may submit. None: any number, on the fake provider only.
    job_limit: int | None = None

    def refusal(self, submitted: int) -> str | None:
        """Why one more job may not be submitted after `submitted` of them, or None."""
        if self.job_limit is not None and submitted >= self.job_limit:
            return f"{self.job_limit} jobs were submitted to the {self.provider_name}: no more"
        return None

    @property
    def url(self) -> str:
        return PROVIDERS[self.provider]

    @property
    def planned_jobs(self) -> int:
        return UNINTERRUPTED + self.runs + len(FAILPOINTS) * self.failpoint_runs

    @property
    def provider_name(self) -> str:
        return "Claude API" if self.provider == "claude" else "fake provider"


def _count(env: Mapping[str, str], name: str, default: int) -> int:
    text = env.get(name, str(default))
    if not (text.isascii() and text.isdecimal()) or int(text) < 1:
        raise ValueError(f"{name} must be a whole number above 0, got {text!r}")
    return int(text)


def settings(env: Mapping[str, str]) -> Settings:
    """The run the environment asks for. Raises ValueError for one that may not start.

    On the Claude API the model has to be named: there is no default that
    costs money. On the fake provider it may not be named: the job runs the
    packaged scenario, and a result would carry a model id that never ran.
    """
    provider = env.get("KILLS_PROVIDER", "fake")
    if provider not in PROVIDERS:
        raise ValueError(f"KILLS_PROVIDER must be one of {', '.join(PROVIDERS)}, got {provider!r}")
    model = env.get("KILLS_MODEL", "")
    if provider == "fake":
        if model:
            raise ValueError("KILLS_MODEL is for the Claude API; the fake provider runs a scenario")
        model = FAKE_MODEL
    elif not model.startswith("claude-"):
        raise ValueError(
            f"KILLS_MODEL must name a Claude model id for a run on the Claude API, got {model!r}"
        )
    window = None
    if "KILLS_WINDOW" in env:
        try:
            window = float(env["KILLS_WINDOW"])
        except ValueError:
            window = -1.0
        if not 0 < window <= MAX_WINDOW:
            raise ValueError(
                f"KILLS_WINDOW must be seconds, above 0 and at most {MAX_WINDOW:g},"
                f" got {env['KILLS_WINDOW']!r}"
            )
    job_limit = API_JOB_LIMIT if provider == "claude" else None
    if "KILLS_JOB_LIMIT" in env:
        job_limit = _count(env, "KILLS_JOB_LIMIT", API_JOB_LIMIT)
        if provider == "claude" and job_limit > API_JOB_LIMIT:
            raise ValueError(
                f"KILLS_JOB_LIMIT is {job_limit}; a run on the Claude API may submit"
                f" at most {API_JOB_LIMIT}"
            )
    found = Settings(
        provider=provider,
        model=model,
        runs=_count(env, "KILLS_RUNS", 20),
        failpoint_runs=_count(env, "KILLS_FAILPOINT_RUNS", 5),
        seed=env.get("KILLS_SEED", "20261119"),
        window=window,
        job_limit=job_limit,
    )
    if provider == "claude" and found.planned_jobs > API_JOB_LIMIT:
        raise ValueError(
            f"{found.planned_jobs} jobs are planned; a run on the Claude API may submit"
            f" at most {API_JOB_LIMIT}"
        )
    return found


def kill_time(seed: str, index: int, window: float) -> float:
    """Seconds after the submission at which run `index` kills the job's worker.

    The seed fixes a fraction of the window for each run, so run N can be
    repeated on its own. The window is measured, so it is recorded with the seed.
    """
    return random.Random(f"{seed}:{index}").uniform(0, window)  # noqa: S311 - not a secret


@dataclass
class Run:
    """One job, and what the records outside its step log say about it."""

    job: dict[str, Any]
    steps: list[dict[str, Any]]
    # The tracker's own issues for the job, by operation id.
    issues: dict[str, list[int]]
    # Approvals the runner gave, by operation id and by tool.
    approvals: dict[str, int] = field(default_factory=dict)
    approved_tools: dict[str, int] = field(default_factory=dict)
    # Seconds from the submission to the final status, on the runner's clock.
    seconds: float = 0.0
    note: str = ""
    # The model the run was started for. Every model request has to name it.
    model: str = ""

    def duplicates(self) -> dict[str, list[int]]:
        """Operation ids with more issues than one plus the approved reruns."""
        return {
            operation: ids
            for operation, ids in self.issues.items()
            if len(ids) > 1 + self.approvals.get(operation, 0)
        }

    def responses(self) -> list[tuple[int, list[str]]]:
        """The lease epoch and the block types of each model response, in the log's order.

        A response whose content could not be stored has no blocks. Anything
        in the list that is not a block with a type is passed over.
        """
        found = []
        for step in self.steps:
            if step["kind"] != "model_response":
                continue
            content = step["payload"].get("content")
            blocks = content if isinstance(content, list) else []
            types = [str(b["type"]) for b in blocks if isinstance(b, dict) and "type" in b]
            found.append((int(step["lease_epoch"]), types))
        return found

    def handed_on(self) -> list[int]:
        """Lease epochs that ended with a wait or a park for an approval, in order.

        The claim that follows such an epoch is not a takeover.
        """
        found = set()
        for step in self.steps:
            payload = step["payload"]
            if (
                step["kind"] == "status_changed"
                and isinstance(payload, dict)
                and payload.get("to") in NOT_A_TAKEOVER
            ):
                found.add(int(step["lease_epoch"]))
        return sorted(found)

    def summary(self) -> str:
        job = self.job
        issues = ", ".join(f"{op.split(':')[-1]}={ids}" for op, ids in self.issues.items()) or "-"
        responses = self.responses()
        # What the thinking counts of a results line are made from.
        epochs = [epoch for epoch, _ in responses]
        thinking = [epoch for epoch, types in responses if THINKING in types]
        return (
            f"{job['id']} {job['status']:<10} takeovers={job['takeovers']}"
            f" claims={job['lease_epoch']} tokens={job['tokens_used']} {self.seconds:.1f}s"
            f" approvals={sum(self.approvals.values())}"
            f" responses at epochs {epochs} thinking at {thinking}"
            f" waited or parked at {self.handed_on()}"
            f" issues: {issues} {self.note}"
        )


def problems(run: Run) -> list[str]:
    """What is wrong with a job by the pass line of every run. Empty when it passes."""
    job, found = run.job, []
    if job["status"] != "succeeded":
        found.append(f"status is {job['status']} ({job.get('error')})")
    if not run.issues:
        found.append("the tracker has no issue for the job")
    duplicates = run.duplicates()
    if duplicates:
        found.append(f"more issues than one plus approved reruns: {duplicates}")
    if job["token_budget"] != TOKEN_BUDGET:
        found.append(f"the job's budget is {job['token_budget']}, not {TOKEN_BUDGET}")
    if job["tokens_used"] > job["token_budget"]:
        found.append(f"{job['tokens_used']} tokens used, the budget is {job['token_budget']}")
    # The step log names the model each request was sent to. A row for one
    # model must not hold a request to another.
    models = {s["payload"].get("model") for s in run.steps if s["kind"] == "model_request"}
    if models - {run.model}:
        found.append(f"model requests to {sorted(map(str, models))}, not {run.model!r}")
    return found


def uninterrupted_problems(run: Run) -> list[str]:
    """The pass line of a job nobody interrupts: no takeover, no approval."""
    found = problems(run)
    if run.job["takeovers"] != 0:
        found.append(f"{run.job['takeovers']} takeovers in a job nobody interrupted")
    if run.approvals:
        found.append(f"asked for approval in a job nobody interrupted: {run.approved_tools}")
    return found


def failpoint_problems(run: Run, failpoint: str, fired: int) -> list[str]:
    """The pass line of a job whose worker stopped at `failpoint` in the call that opens the issue.

    `fired` is how often the workers' logs hold the failpoint line.
    """
    found = problems(run)
    if fired != 1:
        found.append(f"the failpoint stopped a worker {fired} times, not once")
    if run.job["takeovers"] != 1:
        found.append(f"{run.job['takeovers']} takeovers, not one")
    if run.approvals:
        found.append(f"asked for approval: {run.approved_tools}")
    intents = [
        s for s in run.steps if s["kind"] == "tool_intent" and s["payload"].get("name") == TOOL
    ]
    if not intents:
        return [*found, f"the step log has no intent for a {TOOL} call"]
    # The failpoint fires in the job's first call of the tool.
    call = intents[0]["payload"]["tool_use_id"]
    epochs = [s["lease_epoch"] for s in intents if s["payload"]["tool_use_id"] == call]
    results = [
        s
        for s in run.steps
        if s["kind"] == "tool_result" and s["payload"].get("tool_use_id") == call
    ]
    recovered = [s["payload"].get("recovered") is True for s in results]
    if failpoint == "after_tool_ran":
        # The tracker has the issue: the check finds it and the tool does not run again.
        wanted_epochs, wanted_recovered = [1], [True]
    else:
        # The call was never sent: the check finds nothing and the call runs.
        wanted_epochs, wanted_recovered = [1, 2], [False]
    if epochs != wanted_epochs:
        found.append(f"the call's intents are at lease epochs {epochs}, not {wanted_epochs}")
    if recovered != wanted_recovered:
        found.append(f"the call's results have recovered={recovered}, not {wanted_recovered}")
    return found


def block_types(run: Run) -> dict[str, int]:
    """How many blocks of each type the job's model responses hold."""
    counted: dict[str, int] = {}
    for _, types in run.responses():
        for kind in types:
            counted[kind] = counted.get(kind, 0) + 1
    return dict(sorted(counted.items()))


def sent_thinking_back_after_a_takeover(run: Run) -> bool:
    """Whether a worker that took the job over sent a `thinking` block back and was answered.

    A request holds the whole conversation. So a response at a later lease
    epoch than a takeover that came after a `thinking` block means: a worker
    that never received the block rebuilt it from the step log, sent it,
    and the API answered. The job must have succeeded.

    Every claim that hands the job to a worker raises the lease epoch. The
    claim after a wait or a park is not a takeover: its epoch ends with a
    `status_changed` step. A release ends with no step, like a takeover, and
    the job row counts only takeovers. So a job whose unexplained claims
    outnumber its takeovers was also released, and is left out: the count
    may be too low and is never too high.
    """
    job = run.job
    if job["status"] != "succeeded":
        return False
    responses = run.responses()
    first = min((epoch for epoch, types in responses if THINKING in types), default=None)
    if first is None:
        return False
    handed_on = set(run.handed_on())
    # Epoch e ended in a claim of the job at e + 1.
    taken = [e for e in range(1, job["lease_epoch"]) if e not in handed_on]
    if len(taken) != job["takeovers"]:
        return False
    return any(e >= first and any(epoch > e for epoch, _ in responses) for e in taken)


def first_signed_thinking(run: Run) -> int | None:
    """The lease epoch of the first model response with a signed `thinking` block, or None.

    Signed: the block has a `signature` that is text and not empty. That is
    the part the API checks when the block comes back.
    """
    for step in run.steps:
        if step["kind"] != "model_response":
            continue
        content = step["payload"].get("content")
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict) or block.get("type") != THINKING:
                continue
            signature = block.get("signature")
            if isinstance(signature, str) and signature:
                return int(step["lease_epoch"])
    return None


def sent_a_rebuilt_thinking_block(run: Run) -> bool:
    """Whether a later claim of the job wrote a model request after a signed `thinking` block.

    Every claim rebuilds the conversation from the step log, and a request
    holds all of it. So a `model_request` step at a later lease epoch than
    the block means a worker rebuilt the block and went on to send it. Any
    claim counts: a takeover, and the claim after a wait, a park or a release.

    The step is written before the call. A worker stopped between the two
    sent nothing, and the job is still counted here.
    """
    first = first_signed_thinking(run)
    return first is not None and any(
        step["kind"] == "model_request" and int(step["lease_epoch"]) > first for step in run.steps
    )


def answered_after_sending_one(run: Run) -> bool:
    """Whether the API answered a request that held a rebuilt `thinking` block.

    The job sent one, and has a `model_response` step at a later lease
    epoch than the block. The job's status is not asked for: a job that was
    answered and failed later was still answered.
    """
    first = first_signed_thinking(run)
    return (
        first is not None
        and sent_a_rebuilt_thinking_block(run)
        and any(
            step["kind"] == "model_response" and int(step["lease_epoch"]) > first
            for step in run.steps
        )
    )


def refused_after_sending_one(run: Run) -> bool:
    """Whether the job failed because the API refused the signature of a rebuilt block.

    The job sent one, got no answer after it, and failed with the worker's
    words for a 400 and the API's names for the block and its signature. A
    400 that says the block belongs to another conversation is not counted:
    it means the conversation was rebuilt differently, not the signature.
    """
    error = str(run.job.get("error") or "").casefold()
    return (
        sent_a_rebuilt_thinking_block(run)
        and not answered_after_sending_one(run)
        and run.job["status"] == "failed"
        and all(part in error for part in REFUSED)
        and OTHER_CONVERSATION not in error
    )


def errors_of_failed_jobs(runs: list[Run]) -> list[str]:
    """Each different error of the jobs that failed, cut to ERROR_LENGTH characters."""
    return sorted(
        {
            str(run.job.get("error") or "")[:ERROR_LENGTH]
            for run in runs
            if run.job["status"] == "failed"
        }
    )


def thinking_record(runs: list[Run]) -> dict[str, Any]:
    """What a set of jobs shows about `thinking` blocks. No kill run is judged by it.

    A model decides by itself whether it sends such a block, so a count of
    0 is a result and not a failure. The block types of every response are
    recorded with it, so a 0 can be told from a block under another name.

    A switched-off run reads the last three counts (scripts/switched_off.py).
    In a normal run every job that sent a rebuilt block is answered.
    """
    types: dict[str, int] = {}
    for run in runs:
        for kind, count in block_types(run).items():
            types[kind] = types.get(kind, 0) + count
    return {
        "jobs_with_a_thinking_block": sum(THINKING in block_types(run) for run in runs),
        "jobs_that_sent_one_back_after_a_takeover": sum(
            sent_thinking_back_after_a_takeover(run) for run in runs
        ),
        "block_types": dict(sorted(types.items())),
        "jobs_that_sent_a_rebuilt_thinking_block": sum(
            sent_a_rebuilt_thinking_block(run) for run in runs
        ),
        "jobs_answered_after_sending_one": sum(answered_after_sending_one(run) for run in runs),
        "jobs_refused_after_sending_one": sum(refused_after_sending_one(run) for run in runs),
    }


def jobs_taken_over(runs: list[Run]) -> int:
    """Jobs taken over at least once. A job taken over twice counts once."""
    return sum(run.job["takeovers"] > 0 for run in runs)


def too_few_kills(runs: int, sent: int, taken_over: int) -> list[str]:
    """Why a set of random kills shows nothing: too few of them hit a job.

    `taken_over` counts jobs, not takeovers: a job taken over twice is one.
    """
    found = []
    if sent < runs // 2:
        found.append(f"{sent} of {runs} kills were sent to a worker that held the job")
    if taken_over < runs // 4:
        found.append(f"{taken_over} of {runs} jobs were taken over")
    return found
