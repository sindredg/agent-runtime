"""The check of a model response's shape, before any of its tools run."""

from runtime.provider.base import ModelResponse
from runtime.worker.agent import _response_problem


def response(*tool_use_ids: str) -> ModelResponse:
    content = [
        {"type": "tool_use", "id": tool_use_id, "name": "read_file", "input": {"path": "a"}}
        for tool_use_id in tool_use_ids
    ]
    return ModelResponse("msg_1", "m", content, "tool_use", 1, 1)


def test_calls_with_ids_of_their_own_are_no_problem() -> None:
    assert _response_problem(response("t3", "t4"), used_ids={"t1", "t2"}, one_call=False) is None


def test_a_call_id_that_an_earlier_response_used_is_a_problem() -> None:
    """The id names the call's operation. Used twice, two calls would share one operation id."""
    problem = _response_problem(response("t3", "t1"), used_ids={"t1", "t2"}, one_call=False)
    assert problem == "a tool_use block has the id of an earlier call"


def test_the_first_response_has_no_earlier_ids_to_clash_with() -> None:
    assert _response_problem(response("t1"), used_ids=set(), one_call=False) is None


def test_one_call_where_one_was_asked_is_no_problem() -> None:
    assert _response_problem(response("t1"), used_ids=set(), one_call=True) is None
    assert _response_problem(response(), used_ids=set(), one_call=True) is None


def test_more_calls_than_the_one_asked_are_a_problem() -> None:
    problem = _response_problem(response("t1", "t2", "t3"), used_ids=set(), one_call=True)
    assert problem == "it has 3 tool_use blocks where one was asked"
