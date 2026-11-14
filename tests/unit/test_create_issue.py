"""The create_issue tool against a stubbed tracker."""

import json
from pathlib import Path
from typing import Any

import httpx2
import pytest

from runtime.tools import default_tools
from runtime.tools.base import CheckOutcome, ToolCall, ToolResult
from runtime.tools.create_issue import create_issue_tool

URL = "http://tracker.test"
CALL = ToolCall(
    workspace=Path("unused"),
    input={"title": "CI is red", "body": "test_x failed"},
    operation_id="job-1:toolu_1",
)


class Tracker:
    """Answers like the fake tracker and records what it was asked."""

    def __init__(self) -> None:
        self.issues: list[dict[str, Any]] = []
        self.requests: list[httpx2.Request] = []
        self.answer: httpx2.Response | Exception | None = None
        # What a POST runs into, and whether the issue was stored before it did.
        self.post_fails: Exception | None = None
        self.stores_before_failing = False

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if isinstance(self.answer, Exception):
            raise self.answer
        if self.answer is not None:
            return self.answer
        if request.method == "POST":
            if self.post_fails is not None and not self.stores_before_failing:
                raise self.post_fails
            entry = {"id": len(self.issues) + 1, **json.loads(request.content)}
            self.issues.append(entry)
            if self.post_fails is not None:
                raise self.post_fails
            return httpx2.Response(201, json=entry)
        wanted = request.url.params.get("operation_id")
        return httpx2.Response(200, json=[i for i in self.issues if i["operation_id"] == wanted])


@pytest.fixture
def tracker() -> Tracker:
    return Tracker()


def tool(tracker: Tracker) -> Any:
    return create_issue_tool(URL, transport=httpx2.MockTransport(tracker))


def test_it_creates_an_issue_and_sends_the_operation_id(tracker: Tracker) -> None:
    result = tool(tracker).run(CALL)
    assert result == ToolResult("created issue 1: CI is red")
    assert tracker.issues == [
        {"id": 1, "title": "CI is red", "body": "test_x failed", "operation_id": "job-1:toolu_1"}
    ]
    request = tracker.requests[0]
    assert (request.method, str(request.url)) == ("POST", f"{URL}/issues")


def test_it_is_not_idempotent_and_brings_a_check(tracker: Tracker) -> None:
    built = tool(tracker)
    assert built.name == "create_issue"
    assert built.idempotent is False
    assert built.check is not None
    assert set(built.input_schema["required"]) == {"title", "body"}


@pytest.mark.parametrize(
    "tool_input",
    [
        {},
        {"title": "t"},
        {"title": 5, "body": "b"},
        {"title": "", "body": "b"},
        {"title": "t", "body": None},
    ],
)
def test_input_without_a_title_and_a_body_is_an_error_result_and_sends_nothing(
    tracker: Tracker, tool_input: dict[str, Any]
) -> None:
    result = tool(tracker).run(ToolCall(Path("unused"), tool_input, "job-1:toolu_1"))
    assert result.is_error
    assert tracker.requests == []


@pytest.mark.parametrize(
    "answer",
    [
        httpx2.Response(500, text="boom"),
        httpx2.Response(422, json={"detail": "no"}),
        httpx2.Response(201, text="not json"),
        httpx2.Response(201, json={"no": "id"}),
        httpx2.ConnectError("refused"),
        httpx2.ReadTimeout("slow"),
    ],
    ids=["500", "422", "not-json", "no-id", "connect-error", "timeout"],
)
def test_a_tracker_that_fails_gives_an_error_result(tracker: Tracker, answer: Any) -> None:
    tracker.answer = answer
    result = tool(tracker).run(CALL)
    assert result.is_error
    assert "tracker" in result.content


# A request that got no answer: the tracker may have stored the issue all the same

LOST = [httpx2.ReadTimeout("slow"), httpx2.RemoteProtocolError("the connection broke")]
lost_answers = pytest.mark.parametrize("lost", LOST, ids=["timeout", "broken-connection"])


@lost_answers
def test_an_issue_the_tracker_stored_without_answering_is_reported_as_created(
    tracker: Tracker, lost: Exception
) -> None:
    """Told that the call failed, the model may make it again: a new call, a second issue."""
    tracker.post_fails, tracker.stores_before_failing = lost, True
    result = tool(tracker).run(CALL)
    assert result == ToolResult("created issue 1: CI is red")
    # The tool asked for its operation id before it answered.
    assert [request.method for request in tracker.requests] == ["POST", "GET"]
    assert tracker.requests[1].url.params["operation_id"] == "job-1:toolu_1"
    assert len(tracker.issues) == 1


@lost_answers
def test_an_issue_the_tracker_does_not_show_is_an_error_that_says_it_may_exist(
    tracker: Tracker, lost: Exception
) -> None:
    """The request may still arrive after the tool gave up, so not found is not a no."""
    tracker.post_fails = lost
    result = tool(tracker).run(CALL)
    assert result.is_error
    assert "could not be reached" in result.content
    assert "may exist" in result.content
    assert [request.method for request in tracker.requests] == ["POST", "GET"]
    assert tracker.issues == []


@lost_answers
def test_a_tracker_that_cannot_be_asked_either_is_an_error_that_says_the_issue_may_exist(
    tracker: Tracker, lost: Exception
) -> None:
    tracker.answer = lost
    result = tool(tracker).run(CALL)
    assert result.is_error
    assert "may exist" in result.content
    assert [request.method for request in tracker.requests] == ["POST", "GET"]


def test_a_tracker_that_answers_with_an_error_is_not_asked_again(tracker: Tracker) -> None:
    """An answer arrived. The tool reports it and looks nothing up."""
    tracker.answer = httpx2.Response(500, text="boom")
    result = tool(tracker).run(CALL)
    assert result == ToolResult("the tracker answered 500", is_error=True)
    assert [request.method for request in tracker.requests] == ["POST"]


# The check: did this call already create its issue?


def test_the_check_answers_done_with_the_result_when_the_issue_exists(tracker: Tracker) -> None:
    built = tool(tracker)
    created = built.run(CALL)
    answer = built.check(CALL)
    # The same result the run gave, so the model reads the same thing either way.
    assert answer == created
    asked = tracker.requests[-1]
    assert (asked.method, asked.url.path) == ("GET", "/issues")
    assert asked.url.params["operation_id"] == "job-1:toolu_1"
    # Looking did not create anything.
    assert len(tracker.issues) == 1


def test_the_check_answers_not_done_when_the_tracker_has_no_such_issue(tracker: Tracker) -> None:
    built = tool(tracker)
    built.run(ToolCall(Path("unused"), CALL.input, "another-job:toolu_1"))
    assert built.check(CALL) is CheckOutcome.NOT_DONE


@pytest.mark.parametrize(
    "answer",
    [
        httpx2.ConnectError("refused"),
        httpx2.ReadTimeout("slow"),
        httpx2.Response(500, text="boom"),
        httpx2.Response(404, json=[]),
        httpx2.Response(200, text="not json"),
        httpx2.Response(200, json={"not": "a list"}),
        httpx2.Response(200, json=[{"no": "id"}]),
    ],
    ids=["connect-error", "timeout", "500", "404", "not-json", "not-a-list", "entry-without-id"],
)
def test_the_check_answers_unknown_when_the_tracker_cannot_be_asked(
    tracker: Tracker, answer: Any
) -> None:
    """Anything but a clear answer is unknown. Not done would let the call run twice."""
    tracker.answer = answer
    assert tool(tracker).check(CALL) is CheckOutcome.UNKNOWN


def test_the_tool_is_offered_only_when_a_tracker_is_configured() -> None:
    assert default_tools().get("create_issue") is None
    assert default_tools(tracker_url=URL).get("create_issue") is not None
    assert default_tools(tracker_url=URL).get("read_file") is not None
