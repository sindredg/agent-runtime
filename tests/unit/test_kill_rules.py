"""The pass line of the kill runs, checked on made-up jobs.

The runs themselves need the stack, and on the Claude API a key. What they
judge a job by is checked here: each rule gets a job that passes and one
that breaks only that rule.
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

RULES = Path(__file__).resolve().parents[1] / "kills" / "rules.py"
JOB = "5b1e"
CALL = "toolu_1"
OPERATION = f"{JOB}:{CALL}"


@pytest.fixture(scope="module")
def rules() -> ModuleType:
    spec = importlib.util.spec_from_file_location("kill_rules", RULES)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before it runs: the dataclasses in it look their module up.
    sys.modules["kill_rules"] = module
    spec.loader.exec_module(module)
    return module


def job(**changes: Any) -> dict[str, Any]:
    return {
        "id": JOB,
        "status": "succeeded",
        "takeovers": 0,
        # Claims that handed the job to a worker. A claim that ends the job adds 0.
        "lease_epoch": 1,
        "tokens_used": 4_000,
        "token_budget": 50_000,
        "error": None,
        **changes,
    }


def intent(epoch: int, call: str = CALL, name: str = "create_issue") -> dict[str, Any]:
    return {
        "kind": "tool_intent",
        "lease_epoch": epoch,
        "payload": {"name": name, "tool_use_id": call},
    }


def result(epoch: int, call: str = CALL, **more: Any) -> dict[str, Any]:
    payload = {"tool_use_id": call, "content": "created issue 7: CI failed", **more}
    return {"kind": "tool_result", "lease_epoch": epoch, "payload": payload}


# Settings


def test_the_fake_provider_is_the_default(rules: ModuleType) -> None:
    found = rules.settings({})
    assert (found.provider, found.model, found.url) == (
        "fake",
        "ci-triage-slow",
        "http://fake-provider:8000",
    )
    assert (found.runs, found.failpoint_runs, found.planned_jobs) == (20, 5, 33)
    assert found.provider_name == "fake provider"


def test_the_claude_api_needs_a_model_id(rules: ModuleType) -> None:
    with pytest.raises(ValueError, match="KILLS_MODEL must name a Claude model id"):
        rules.settings({"KILLS_PROVIDER": "claude"})
    # A scenario name would be sent to the API as a model id.
    with pytest.raises(ValueError, match="KILLS_MODEL must name a Claude model id"):
        rules.settings({"KILLS_PROVIDER": "claude", "KILLS_MODEL": "ci-triage-slow"})
    found = rules.settings({"KILLS_PROVIDER": "claude", "KILLS_MODEL": "claude-haiku-5-5"})
    assert (found.model, found.url) == ("claude-haiku-5-5", "https://api.anthropic.com")
    assert found.provider_name == "Claude API"


def test_the_fake_provider_takes_no_model_id(rules: ModuleType) -> None:
    with pytest.raises(ValueError, match="KILLS_MODEL is for the Claude API"):
        rules.settings({"KILLS_MODEL": "claude-haiku-5-5"})


def test_an_unknown_provider_is_refused(rules: ModuleType) -> None:
    with pytest.raises(ValueError, match="KILLS_PROVIDER must be one of fake, claude"):
        rules.settings({"KILLS_PROVIDER": "openai"})


def test_a_run_on_the_api_may_not_plan_more_than_the_limit(rules: ModuleType) -> None:
    api = {"KILLS_PROVIDER": "claude", "KILLS_MODEL": "claude-haiku-5-5"}
    assert rules.settings({**api, "KILLS_RUNS": "27"}).planned_jobs == 40
    with pytest.raises(ValueError, match="41 jobs are planned"):
        rules.settings({**api, "KILLS_RUNS": "28"})
    # The fake provider costs nothing: the 200-run size is allowed there.
    assert rules.settings({"KILLS_RUNS": "200"}).planned_jobs == 213


# "0": a run with no kills would pass and record nothing.
@pytest.mark.parametrize("text", ["-1", "2.5", "many", "", "0", "\u0663"])
def test_a_count_that_is_not_a_whole_number_is_refused(rules: ModuleType, text: str) -> None:
    with pytest.raises(ValueError, match="KILLS_RUNS must be a whole number"):
        rules.settings({"KILLS_RUNS": text})


def test_a_window_is_measured_unless_it_is_given(rules: ModuleType) -> None:
    """A recorded run is repeated with its seed and the window it measured."""
    assert rules.settings({}).window is None
    assert rules.settings({"KILLS_WINDOW": "7.25"}).window == 7.25


@pytest.mark.parametrize("text", ["0", "-2", "301", "nan", "inf", "soon", ""])
def test_a_window_that_is_not_a_time_in_range_is_refused(rules: ModuleType, text: str) -> None:
    with pytest.raises(ValueError, match="KILLS_WINDOW must be seconds"):
        rules.settings({"KILLS_WINDOW": text})


# Kill times


def test_a_kill_time_is_a_fixed_fraction_of_the_window(rules: ModuleType) -> None:
    times = [rules.kill_time("s", index, 10.0) for index in range(50)]
    assert times == [rules.kill_time("s", index, 10.0) for index in range(50)]
    assert all(0 <= t <= 10.0 for t in times)
    assert len(set(times)) == 50
    # Twice the window, twice the time: the seed fixes the fraction.
    assert rules.kill_time("s", 3, 20.0) == pytest.approx(2 * times[3])
    assert rules.kill_time("other", 3, 10.0) != times[3]


# Every job


def test_a_job_that_succeeded_with_one_issue_passes(rules: ModuleType) -> None:
    assert rules.problems(rules.Run(job(), [], {OPERATION: [7]})) == []


def test_a_job_that_did_not_succeed_fails(rules: ModuleType) -> None:
    run = rules.Run(job(status="failed", error="the model declined the request"), [], {})
    found = rules.problems(run)
    assert "status is failed (the model declined the request)" in found


def test_a_job_with_no_issue_fails(rules: ModuleType) -> None:
    """A model that never calls the tool shows nothing about the call."""
    assert rules.problems(rules.Run(job(), [], {})) == ["the tracker has no issue for the job"]


def test_two_issues_for_one_operation_fail(rules: ModuleType) -> None:
    found = rules.problems(rules.Run(job(), [], {OPERATION: [7, 8]}))
    assert found == [f"more issues than one plus approved reruns: {{'{OPERATION}': [7, 8]}}"]


def test_an_approved_rerun_is_not_a_duplicate(rules: ModuleType) -> None:
    run = rules.Run(job(), [], {OPERATION: [7, 8]}, approvals={OPERATION: 1})
    assert rules.problems(run) == []
    # One approval covers one rerun, not two.
    run = rules.Run(job(), [], {OPERATION: [7, 8, 9]}, approvals={OPERATION: 1})
    assert len(rules.problems(run)) == 1


def test_two_operations_with_one_issue_each_pass(rules: ModuleType) -> None:
    """A model may open two issues. That is two calls, not one call twice."""
    run = rules.Run(job(), [], {OPERATION: [7], f"{JOB}:toolu_2": [8]})
    assert rules.problems(run) == []


def test_tokens_over_the_budget_fail(rules: ModuleType) -> None:
    found = rules.problems(rules.Run(job(tokens_used=50_001), [], {OPERATION: [7]}))
    assert found == ["50001 tokens used, the budget is 50000"]
    assert rules.problems(rules.Run(job(tokens_used=50_000), [], {OPERATION: [7]})) == []


def test_a_job_with_another_budget_fails(rules: ModuleType) -> None:
    """The cost bound of a run rests on every job carrying this budget."""
    found = rules.problems(rules.Run(job(token_budget=1_000_000), [], {OPERATION: [7]}))
    assert found == ["the job's budget is 1000000, not 50000"]


# Jobs nobody interrupts


def test_an_uninterrupted_job_has_no_takeover_and_no_approval(rules: ModuleType) -> None:
    assert rules.uninterrupted_problems(rules.Run(job(), [], {OPERATION: [7]})) == []
    taken = rules.Run(job(takeovers=1), [], {OPERATION: [7]})
    assert rules.uninterrupted_problems(taken) == ["1 takeovers in a job nobody interrupted"]
    asked = rules.Run(
        job(), [], {OPERATION: [7]}, approvals={"x": 1}, approved_tools={"run_shell": 1}
    )
    assert rules.uninterrupted_problems(asked) == [
        "asked for approval in a job nobody interrupted: {'run_shell': 1}"
    ]


# Failpoint runs


def after_ran(rules: ModuleType, **changes: Any) -> Any:
    """A job whose worker stopped after the tool ran: the check finds the issue."""
    steps = [intent(1), result(2, recovered=True)]
    return rules.Run(job(takeovers=1), steps, {OPERATION: [7]}, **changes)


def after_intent(rules: ModuleType) -> Any:
    """A job whose worker stopped before the tool ran: the call is run by the next worker."""
    steps = [intent(1), intent(2), result(2)]
    return rules.Run(job(takeovers=1), steps, {OPERATION: [7]})


def test_the_two_failpoints_pass_with_their_own_shape(rules: ModuleType) -> None:
    assert rules.failpoint_problems(after_ran(rules), "after_tool_ran", 1) == []
    assert rules.failpoint_problems(after_intent(rules), "after_tool_intent", 1) == []


def test_each_failpoint_fails_with_the_others_shape(rules: ModuleType) -> None:
    assert rules.failpoint_problems(after_ran(rules), "after_tool_intent", 1) == [
        "the call's intents are at lease epochs [1], not [1, 2]",
        "the call's results have recovered=[True], not [False]",
    ]
    assert rules.failpoint_problems(after_intent(rules), "after_tool_ran", 1) == [
        "the call's intents are at lease epochs [1, 2], not [1]",
        "the call's results have recovered=[False], not [True]",
    ]


@pytest.mark.parametrize("fired", [0, 2])
def test_a_failpoint_that_did_not_fire_once_fails(rules: ModuleType, fired: int) -> None:
    found = rules.failpoint_problems(after_ran(rules), "after_tool_ran", fired)
    assert found == [f"the failpoint stopped a worker {fired} times, not once"]


def test_a_failpoint_run_has_one_takeover_and_no_approval(rules: ModuleType) -> None:
    run = after_ran(rules)
    run.job["takeovers"] = 2
    assert rules.failpoint_problems(run, "after_tool_ran", 1) == ["2 takeovers, not one"]
    run = after_ran(rules, approvals={OPERATION: 1}, approved_tools={"create_issue": 1})
    assert rules.failpoint_problems(run, "after_tool_ran", 1) == [
        "asked for approval: {'create_issue': 1}"
    ]


def test_a_skipped_intent_shows_as_a_duplicate(rules: ModuleType) -> None:
    """The run with the intent write switched off: the call ran twice and nothing says so.

    The worker stops after the tool ran. The next worker finds no intent in
    the log and runs the call as if for the first time.
    """
    run = rules.Run(job(takeovers=1), [result(2)], {OPERATION: [7, 8]})
    assert rules.failpoint_problems(run, "after_tool_ran", 1) == [
        f"more issues than one plus approved reruns: {{'{OPERATION}': [7, 8]}}",
        "the step log has no intent for a create_issue call",
    ]


def test_the_failpoint_is_judged_on_the_first_call_of_the_tool(rules: ModuleType) -> None:
    """A second issue the model opens later is a call of its own."""
    steps = [
        intent(1, "toolu_0", name="read_file"),
        result(1, "toolu_0"),
        intent(1),
        result(2, recovered=True),
        intent(2, "toolu_2"),
        result(2, "toolu_2"),
    ]
    run = rules.Run(job(takeovers=1), steps, {OPERATION: [7], f"{JOB}:toolu_2": [8]})
    assert rules.failpoint_problems(run, "after_tool_ran", 1) == []


# The floor on kills


def test_kills_that_hit_no_job_show_nothing(rules: ModuleType) -> None:
    assert rules.too_few_kills(20, 10, 5) == []
    assert rules.too_few_kills(20, 9, 5) == [
        "9 of 20 kills were sent to a worker that held the job"
    ]
    assert rules.too_few_kills(20, 10, 4) == ["4 of 20 jobs were taken over"]


def test_a_job_taken_over_twice_counts_once(rules: ModuleType) -> None:
    runs = [rules.Run(job(takeovers=n), [], {OPERATION: [7]}) for n in (2, 0, 1)]
    assert rules.jobs_taken_over(runs) == 2


# The model


def request(model: str) -> dict[str, Any]:
    return {"kind": "model_request", "lease_epoch": 1, "payload": {"model": model}}


def test_every_request_goes_to_the_model_of_the_run(rules: ModuleType) -> None:
    steps = [request("claude-opus-5-5"), request("claude-opus-5-5")]
    run = rules.Run(job(), steps, {OPERATION: [7]}, model="claude-opus-5-5")
    assert rules.problems(run) == []
    steps[1] = request("claude-haiku-5-5")
    assert rules.problems(run) == [
        "model requests to ['claude-haiku-5-5', 'claude-opus-5-5'], not 'claude-opus-5-5'"
    ]


def test_a_run_that_names_no_model_fails_every_job_with_a_request(rules: ModuleType) -> None:
    run = rules.Run(job(), [request("ci-triage-slow")], {OPERATION: [7]})
    assert rules.problems(run) == ["model requests to ['ci-triage-slow'], not ''"]


def test_a_summary_names_the_job_and_its_issues(rules: ModuleType) -> None:
    run = rules.Run(job(), [], {OPERATION: [7]}, seconds=3.21, note="kill at 1.00s")
    assert run.summary() == (
        f"{JOB} succeeded  takeovers=0 claims=1 tokens=4000 3.2s approvals=0"
        f" responses at epochs [] thinking at [] waited or parked at []"
        f" issues: {CALL}=[7] kill at 1.00s"
    )


def test_a_summary_shows_what_the_thinking_counts_are_made_from(rules: ModuleType) -> None:
    """The counts of a results line can be worked out again from the lines of its jobs."""
    steps = [
        request("claude-fable-5-2"),
        response(1, THINKING, CALL_BLOCK),
        response(1, CALL_BLOCK),
        intent(1),
        # Only responses are listed: a result has content too, and is not one.
        result(2),
        response(2, TEXT),
    ]
    run = rules.Run(job(takeovers=1, lease_epoch=2), steps, {OPERATION: [7]}, seconds=9.0)
    assert " takeovers=1 claims=2 " in run.summary()
    assert " responses at epochs [1, 1, 2] thinking at [1] waited or parked at [] " in run.summary()
    steps.insert(2, changed(1, "waiting"))
    assert " waited or parked at [1] " in run.summary()


# Thinking blocks: recorded, never judged

THINKING = {"type": "thinking", "thinking": "", "signature": "sig"}
TEXT = {"type": "text", "text": "Done."}
CALL_BLOCK = {"type": "tool_use", "id": CALL, "name": "create_issue", "input": {}}


def changed(epoch: int, to: str) -> dict[str, Any]:
    """A `status_changed` step as a worker writes it when it hands the job on."""
    payload = {"from": "running", "to": to, "cause": "provider_busy"}
    return {"kind": "status_changed", "lease_epoch": epoch, "payload": payload}


def response(epoch: int, *blocks: Any, **more: Any) -> dict[str, Any]:
    """A `model_response` step as the worker stores it: the blocks under `content`."""
    payload = {"id": "msg_1", "content": list(blocks), "stop_reason": "tool_use", **more}
    return {"kind": "model_response", "lease_epoch": epoch, "payload": payload}


def thinking_job(rules: ModuleType, steps: list[dict[str, Any]], **changes: Any) -> Any:
    return rules.Run(job(**changes), steps, {OPERATION: [7]})


def test_blocks_are_counted_by_type_over_every_response(rules: ModuleType) -> None:
    steps = [
        request("claude-fable-5-2"),
        response(1, THINKING, TEXT, CALL_BLOCK),
        intent(1),
        result(1),
        response(1, THINKING, TEXT),
    ]
    assert rules.block_types(thinking_job(rules, steps)) == {
        "text": 2,
        "thinking": 2,
        "tool_use": 1,
    }
    assert rules.block_types(thinking_job(rules, [intent(1), result(1)])) == {}


def test_a_response_that_was_not_stored_whole_counts_what_is_there(rules: ModuleType) -> None:
    """A response whose content could not be stored has an empty list and a flag."""
    steps = [
        response(1, content_dropped=True),
        {"kind": "model_response", "lease_epoch": 1, "payload": {"stop_reason": "end_turn"}},
        response(1, "not a block", {"no": "type"}, TEXT),
    ]
    assert rules.block_types(thinking_job(rules, steps)) == {"text": 1}


def test_a_block_sent_back_after_a_takeover_and_answered_counts(rules: ModuleType) -> None:
    """The worker that took over rebuilt the block from the step log, and the API answered."""
    steps = [response(1, THINKING, CALL_BLOCK), intent(1), result(2), response(2, TEXT)]
    run = thinking_job(rules, steps, takeovers=1, lease_epoch=2)
    assert rules.sent_thinking_back_after_a_takeover(run) is True


def test_a_job_nobody_took_over_sent_nothing_back_after_a_takeover(rules: ModuleType) -> None:
    steps = [response(1, THINKING, CALL_BLOCK), intent(1), result(1), response(1, TEXT)]
    assert rules.sent_thinking_back_after_a_takeover(thinking_job(rules, steps)) is False


def test_a_job_without_a_thinking_block_does_not_count(rules: ModuleType) -> None:
    steps = [response(1, CALL_BLOCK), intent(1), result(2), response(2, TEXT)]
    run = thinking_job(rules, steps, takeovers=1, lease_epoch=2)
    assert rules.sent_thinking_back_after_a_takeover(run) is False


def test_a_takeover_before_the_block_does_not_count(rules: ModuleType) -> None:
    """The block first came to the worker that took over. Nobody rebuilt it from the log."""
    steps = [
        request("m"),
        response(2, THINKING, CALL_BLOCK),
        intent(2),
        result(2),
        response(2, TEXT),
    ]
    run = thinking_job(rules, steps, takeovers=1, lease_epoch=2)
    assert rules.sent_thinking_back_after_a_takeover(run) is False


def test_a_takeover_after_the_last_answer_does_not_count(rules: ModuleType) -> None:
    """The next worker found the job finished and sent no request."""
    steps = [response(1, THINKING, CALL_BLOCK), intent(1), result(1), response(1, THINKING, TEXT)]
    run = thinking_job(rules, steps, takeovers=1, lease_epoch=2)
    assert rules.sent_thinking_back_after_a_takeover(run) is False


def test_a_block_in_both_responses_counts_from_the_first(rules: ModuleType) -> None:
    """A model that thinks in every answer: the takeover after the first answer counts."""
    steps = [response(1, THINKING, CALL_BLOCK), intent(1), result(2), response(2, THINKING, TEXT)]
    run = thinking_job(rules, steps, takeovers=1, lease_epoch=2)
    assert rules.sent_thinking_back_after_a_takeover(run) is True


def test_a_claim_after_a_wait_is_not_a_takeover(rules: ModuleType) -> None:
    """The lease epoch also rises when a job is claimed after a wait or an approval."""
    steps = [response(1, THINKING, CALL_BLOCK), intent(1), result(1), changed(1, "waiting")]
    waited = thinking_job(rules, [*steps, response(2, TEXT)], takeovers=0, lease_epoch=2)
    assert rules.sent_thinking_back_after_a_takeover(waited) is False
    parked = [*steps[:3], changed(1, "needs_approval"), response(2, TEXT)]
    run = thinking_job(rules, parked, takeovers=0, lease_epoch=2)
    assert rules.sent_thinking_back_after_a_takeover(run) is False


def test_a_takeover_after_a_wait_counts(rules: ModuleType) -> None:
    """The wait's step names its epoch, so the claim after the next epoch is the takeover."""
    steps = [
        response(1, THINKING, CALL_BLOCK),
        changed(1, "waiting"),
        response(2, CALL_BLOCK),
        intent(2),
        result(3),
        response(3, TEXT),
    ]
    run = thinking_job(rules, steps, takeovers=1, lease_epoch=3)
    assert rules.sent_thinking_back_after_a_takeover(run) is True


def test_a_takeover_only_before_a_wait_and_the_block_does_not_count(rules: ModuleType) -> None:
    """Taken over at 1 to 2, the block at 2, a wait at 2: no takeover after the block."""
    steps = [response(2, THINKING, CALL_BLOCK), changed(2, "waiting"), response(3, TEXT)]
    run = thinking_job(rules, steps, takeovers=1, lease_epoch=3)
    assert rules.sent_thinking_back_after_a_takeover(run) is False


def test_a_job_that_was_also_released_is_left_out(rules: ModuleType) -> None:
    """A release leaves no step, like a takeover. More unexplained claims than takeovers."""
    steps = [response(1, THINKING, CALL_BLOCK), intent(1), result(2), response(3, TEXT)]
    run = thinking_job(rules, steps, takeovers=1, lease_epoch=3)
    assert rules.sent_thinking_back_after_a_takeover(run) is False


def test_a_job_that_did_not_succeed_does_not_count(rules: ModuleType) -> None:
    steps = [response(1, THINKING, CALL_BLOCK), intent(1), result(2), response(2, TEXT)]
    run = thinking_job(rules, steps, takeovers=1, lease_epoch=2, status="failed")
    assert rules.sent_thinking_back_after_a_takeover(run) is False


def test_a_results_line_gets_the_counts_of_its_jobs(rules: ModuleType) -> None:
    sent_back = thinking_job(
        rules,
        [response(1, THINKING, CALL_BLOCK), intent(1), result(2), response(2, TEXT)],
        takeovers=1,
        lease_epoch=2,
    )
    not_taken_over = thinking_job(rules, [response(1, THINKING, CALL_BLOCK), response(1, TEXT)])
    no_block = thinking_job(rules, [response(1, CALL_BLOCK), response(1, TEXT)])
    assert rules.thinking_record([sent_back, not_taken_over, no_block]) == {
        "jobs_with_a_thinking_block": 2,
        "jobs_that_sent_one_back_after_a_takeover": 1,
        "block_types": {"text": 3, "thinking": 2, "tool_use": 3},
    }
    assert rules.thinking_record([]) == {
        "jobs_with_a_thinking_block": 0,
        "jobs_that_sent_one_back_after_a_takeover": 0,
        "block_types": {},
    }
