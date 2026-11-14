"""Replay turns a step log into the conversation and the next thing to do."""

import dataclasses
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from hypothesis import given
from hypothesis import strategies as st

from fake_provider.requests import check_request
from runtime.store.steps import Step, StepKind
from runtime.worker.replay import replay

JOB = UUID("00000000-0000-7000-8000-000000000001")
PROMPT = {"role": "user", "content": "go"}


def log(*entries: tuple[StepKind, dict[str, Any]]) -> list[Step]:
    return [
        Step(JOB, seq, kind, payload, 1, datetime.now(UTC))
        for seq, (kind, payload) in enumerate(entries, start=1)
    ]


def request(messages: int = 1) -> tuple[StepKind, dict[str, Any]]:
    return StepKind.MODEL_REQUEST, {"model": "m", "max_tokens": 10, "messages": messages}


def text(value: str) -> dict[str, Any]:
    return {"type": "text", "text": value}


def call(tool_use_id: str) -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_use_id, "name": "read_file", "input": {"path": "a"}}


def response(*content: dict[str, Any], stop: str | None = None) -> tuple[StepKind, dict[str, Any]]:
    calls = any(block["type"] == "tool_use" for block in content)
    return StepKind.MODEL_RESPONSE, {
        "id": "msg",
        "content": list(content),
        "stop_reason": stop or ("tool_use" if calls else "end_turn"),
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def intent(tool_use_id: str) -> tuple[StepKind, dict[str, Any]]:
    return StepKind.TOOL_INTENT, {"tool_use_id": tool_use_id, "name": "read_file", "input": {}}


def result(tool_use_id: str, content: str = "ok", is_error: bool = False) -> Any:
    return StepKind.TOOL_RESULT, {
        "tool_use_id": tool_use_id,
        "content": content,
        "is_error": is_error,
    }


def result_block(tool_use_id: str, content: str = "ok", is_error: bool = False) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content,
        "is_error": is_error,
    }


def test_an_empty_log_is_the_prompt_and_asks_for_the_model() -> None:
    state = replay("go", [])
    assert state.messages == [PROMPT]
    assert state.pending == []
    assert state.stop_reason is None
    assert state.needs_model_call


def test_a_request_without_a_response_still_asks_for_the_model() -> None:
    state = replay("go", log(request()))
    assert state.messages == [PROMPT]
    assert state.needs_model_call


def test_a_tool_call_is_pending_until_its_result() -> None:
    steps = log(request(), response(text("reading"), call("t1")))
    state = replay("go", steps)
    assert state.messages == [
        PROMPT,
        {"role": "assistant", "content": [text("reading"), call("t1")]},
    ]
    assert state.pending == [call("t1")]
    assert state.stop_reason == "tool_use"
    assert not state.needs_model_call

    # The intent alone changes nothing: the call is still open.
    state = replay("go", [*steps, *log(intent("t1"))])
    assert state.pending == [call("t1")]
    assert not state.needs_model_call


def test_results_of_one_assistant_message_share_one_user_message() -> None:
    steps = log(
        request(),
        response(call("t1"), call("t2")),
        intent("t1"),
        result("t1", "one"),
    )
    half = replay("go", steps)
    assert half.pending == [call("t2")]
    assert not half.needs_model_call

    done = replay("go", [*steps, *log(intent("t2"), result("t2", "two", is_error=True))])
    assert done.pending == []
    assert done.needs_model_call
    assert done.messages == [
        PROMPT,
        {"role": "assistant", "content": [call("t1"), call("t2")]},
        {"role": "user", "content": [result_block("t1", "one"), result_block("t2", "two", True)]},
    ]


def test_two_rounds_then_the_final_answer() -> None:
    state = replay(
        "go",
        log(
            request(),
            response(call("t1")),
            intent("t1"),
            result("t1"),
            request(3),
            response(call("t2")),
            intent("t2"),
            result("t2"),
            request(5),
            response(text("done")),
        ),
    )
    assert [message["role"] for message in state.messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert state.messages[-1] == {"role": "assistant", "content": [text("done")]}
    assert state.stop_reason == "end_turn"
    assert state.pending == []
    assert not state.needs_model_call


def test_the_call_ids_of_every_response_are_remembered() -> None:
    state = replay(
        "go",
        log(
            request(),
            response(call("t1"), call("t2")),
            intent("t1"),
            result("t1"),
            intent("t2"),
            result("t2"),
            request(3),
            response(call("t3")),
        ),
    )
    assert state.used_ids == {"t1", "t2", "t3"}
    # Answered or not: only t3 is still open.
    assert state.pending == [call("t3")]


def test_error_steps_do_not_enter_the_conversation() -> None:
    state = replay("go", log(request(), (StepKind.ERROR, {"message": "boom"})))
    assert state.messages == [PROMPT]


def test_a_cut_off_response_keeps_its_stop_reason_and_asks_for_nothing() -> None:
    state = replay("go", log(request(), response(call("t1"), stop="max_tokens")))
    assert state.stop_reason == "max_tokens"
    assert not state.needs_model_call


rounds = st.lists(st.integers(min_value=1, max_value=4), max_size=5)


@given(rounds=rounds, answered=st.booleans(), final=st.booleans())
def test_any_log_replays_to_a_conversation_the_provider_accepts(
    rounds: list[int], answered: bool, final: bool
) -> None:
    """Build a log of tool rounds, cut at a step boundary or not, and replay it.

    Whenever replay says the model is to be called, the conversation is a
    valid Messages request. The fake provider's request check is the judge:
    it refuses a tool_use that has no tool_result.
    """
    entries: list[tuple[StepKind, dict[str, Any]]] = []
    for number, calls in enumerate(rounds):
        ids = [f"t{number}_{index}" for index in range(calls)]
        entries += [request(), response(*(call(i) for i in ids))]
        last = number == len(rounds) - 1
        for position, tool_use_id in enumerate(ids):
            if last and not answered and position == len(ids) - 1:
                entries.append(intent(tool_use_id))
                break
            entries += [intent(tool_use_id), result(tool_use_id)]
    if final and (answered or not rounds):
        entries += [request(), response(text("done"))]

    state = replay("go", log(*entries))
    roles = [message["role"] for message in state.messages]
    assert roles == ["user", "assistant"] * (len(roles) // 2) + ["user"] * (len(roles) % 2)
    open_call = bool(rounds) and not answered
    assert bool(state.pending) == open_call
    finished = final and (answered or not rounds)
    assert state.needs_model_call == (not open_call and not finished)
    if state.needs_model_call:
        check_request({"model": "m", "max_tokens": 1, "messages": state.messages})


# Interrupted calls: what a worker finds when it takes a job over


def approval_requested(tool_use_id: str) -> tuple[StepKind, dict[str, Any]]:
    return StepKind.APPROVAL_REQUESTED, {"tool_use_id": tool_use_id, "name": "read_file"}


def approval_given(tool_use_id: str) -> tuple[StepKind, dict[str, Any]]:
    return StepKind.APPROVAL_GIVEN, {"tool_use_id": tool_use_id}


def test_a_request_without_a_response_is_not_an_interrupted_tool_call() -> None:
    state = replay("go", log(request()))
    assert state.interrupted is None
    assert state.needs_model_call


def test_an_intent_without_a_result_is_reported_as_interrupted() -> None:
    state = replay("go", log(request(), response(call("t1")), intent("t1")))
    assert state.interrupted == {"tool_use_id": "t1", "name": "read_file", "input": {}}
    assert state.approval is None
    assert state.pending == [call("t1")]
    assert not state.needs_model_call


def test_the_interrupted_call_carries_the_time_of_its_last_intent() -> None:
    """A takeover counts the wait for the call's container from it (ADR 0008)."""
    steps = log(request(), response(call("t1")), intent("t1"), intent("t1"))
    first, second = datetime(2026, 1, 1, 12, tzinfo=UTC), datetime(2026, 1, 1, 13, tzinfo=UTC)
    steps[2] = dataclasses.replace(steps[2], created_at=first)
    steps[3] = dataclasses.replace(steps[3], created_at=second)

    assert replay("go", steps).interrupted_at == second
    assert replay("go", [*steps, *log(result("t1"))]).interrupted_at is None


def test_a_result_closes_the_intent() -> None:
    state = replay("go", log(request(), response(call("t1")), intent("t1"), result("t1")))
    assert state.interrupted is None
    assert state.needs_model_call


def test_the_second_of_two_calls_is_the_interrupted_one() -> None:
    state = replay(
        "go",
        log(request(), response(call("t1"), call("t2")), intent("t1"), result("t1"), intent("t2")),
    )
    assert state.interrupted is not None
    assert state.interrupted["tool_use_id"] == "t2"
    # The first call keeps its result. Only the second is still open.
    assert state.pending == [call("t2")]
    assert state.messages[-1] == {"role": "user", "content": [result_block("t1")]}


def test_a_second_intent_for_the_same_call_is_still_one_interrupted_call() -> None:
    """A rerun was itself interrupted: two intents, no result."""
    state = replay("go", log(request(), response(call("t1")), intent("t1"), intent("t1")))
    assert state.interrupted is not None
    assert state.interrupted["tool_use_id"] == "t1"
    assert state.pending == [call("t1")]


def test_approval_steps_are_reported_with_the_interrupted_call() -> None:
    asked = log(request(), response(call("t1")), intent("t1"), approval_requested("t1"))
    state = replay("go", asked)
    assert state.interrupted is not None
    assert state.approval == "requested"

    state = replay("go", [*asked, *log(approval_given("t1"))])
    assert state.interrupted is not None
    assert state.approval == "given"
    # Approval changes nothing in the conversation.
    assert state.pending == [call("t1")]
    assert len(state.messages) == 2


def test_a_new_intent_after_an_approval_starts_without_one() -> None:
    """The approved rerun got its own intent. If it is interrupted too, a person is asked again."""
    state = replay(
        "go",
        log(
            request(),
            response(call("t1")),
            intent("t1"),
            approval_requested("t1"),
            approval_given("t1"),
            intent("t1"),
        ),
    )
    assert state.interrupted is not None
    assert state.approval is None


def test_a_recovered_result_enters_the_conversation_like_any_other() -> None:
    recovered = (
        StepKind.TOOL_RESULT,
        {"tool_use_id": "t1", "content": "found", "is_error": False, "recovered": True},
    )
    state = replay("go", log(request(), response(call("t1")), intent("t1"), recovered))
    assert state.interrupted is None
    assert state.messages[-1] == {"role": "user", "content": [result_block("t1", "found")]}


def test_folding_one_step_at_a_time_gives_the_same_state_as_a_full_replay() -> None:
    steps = log(
        request(),
        response(call("t1"), call("t2")),
        intent("t1"),
        result("t1"),
        intent("t2"),
        approval_requested("t2"),
        approval_given("t2"),
        intent("t2"),
        result("t2"),
        request(3),
        response(text("done")),
    )
    state = replay("go", [])
    for count, step in enumerate(steps, start=1):
        state.apply(step)
        assert state == replay("go", steps[:count])
        assert state.last_seq == count


def test_a_response_recorded_with_a_problem_is_reported_and_yields_no_calls() -> None:
    """Its content is stored as it came and need not have the shape of a message."""
    kind, payload = response(call("t1"))
    for content in (["not a block"], "not a list", [call("t1")]):
        broken = {**payload, "content": content, "problem": "its content is not a list of blocks"}
        state = replay("go", log(request(), (kind, broken)))
        assert state.problem == "its content is not a list of blocks"
        assert state.pending == []
        assert not state.needs_model_call


def test_a_later_response_without_a_problem_clears_it() -> None:
    kind, payload = response(text("hm"))
    state = replay("go", log(request(), (kind, {**payload, "problem": "x"}), response(text("ok"))))
    assert state.problem is None
