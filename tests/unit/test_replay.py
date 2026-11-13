"""Replay turns a step log into the conversation and the next thing to do."""

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
