"""create_issue on Gitea, against a stub that pages and filters like Gitea 28.

The check is the part that can open a second issue: "not done" lets an
interrupted call run again. These tests hold it to "not done" only after a
complete, steady list, and to "unknown" for everything else (ADR 0019).
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2
import pytest

from runtime.tools.base import CheckOutcome, ToolCall, ToolResult
from runtime.tools.create_issue import create_issue_tool
from runtime.tools.trackers import MARKER, MAX_PAGES, PAGE, GiteaTracker, marked, marks

URL, REPO, BOT, TOKEN = "http://gitea.test", "lab-admin/shop", "lab-bot", "token123"
ISSUES = f"/api/v1/repos/{REPO}/issues"


def call(operation_id: str = "job-1:toolu_1", body: str = "test_x failed") -> ToolCall:
    return ToolCall(Path("unused"), {"title": "CI is red", "body": body}, operation_id, 1)


class Gitea:
    """Stores issues, answers a POST with 201, a list with pages and X-Total-Count,
    and `/user` with the token's user. Names are matched without case, as Gitea does."""

    def __init__(self) -> None:
        self.issues: list[dict[str, Any]] = []
        self.requests: list[httpx2.Request] = []
        # The user the token belongs to, as Gitea spells it.
        self.login = BOT
        # Replaces the answer to a list request, or to `/user`.
        self.answer: httpx2.Response | Exception | None = None
        self.user_answer: httpx2.Response | None = None
        self.page_answer: dict[int, httpx2.Response] = {}
        # Added to the true count in X-Total-Count.
        self.count_offset = 0
        # Runs after a page was answered, with the page number.
        self.after_page: Callable[[int], None] = lambda page: None

    def add(self, body: str, author: str = BOT, title: str = "t") -> None:
        number = len(self.issues) + 1
        self.issues.append(
            {"id": 1000 + number, "number": number, "title": title, "body": body,
             "user": {"login": author}, "pull_request": None}
        )  # fmt: skip

    def issue_requests(self) -> list[httpx2.Request]:
        return [r for r in self.requests if r.url.path == ISSUES]

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        assert request.headers["Authorization"] == f"token {TOKEN}"
        if request.url.path == "/api/v1/user":
            return self.user_answer or httpx2.Response(200, json={"login": self.login})
        assert request.url.path == ISSUES
        if request.method == "POST":
            sent = json.loads(request.content)
            self.add(sent["body"], author=self.login, title=sent["title"])
            return httpx2.Response(201, json=self.issues[-1])
        if isinstance(self.answer, Exception):
            raise self.answer
        if self.answer is not None:
            return self.answer
        params = request.url.params
        page, limit = int(params["page"]), min(int(params["limit"]), PAGE)
        if page in self.page_answer:
            return self.page_answer[page]
        wanted = params["created_by"].lower()
        mine = [i for i in reversed(self.issues) if i["user"]["login"].lower() == wanted]
        answer = httpx2.Response(
            200,
            json=mine[(page - 1) * limit : page * limit],
            headers={"X-Total-Count": str(len(mine) + self.count_offset)},
        )
        self.after_page(page)
        return answer


@pytest.fixture
def gitea() -> Gitea:
    return Gitea()


def tool(gitea: Gitea) -> Any:
    return create_issue_tool(GiteaTracker(URL, REPO, TOKEN), transport=httpx2.MockTransport(gitea))


def fill(gitea: Gitea, count: int) -> None:
    for n in range(count):
        gitea.add(marked("b", f"job-x:toolu_{n}"))


def test_it_opens_the_issue_with_the_operation_id_as_the_last_line(gitea: Gitea) -> None:
    result = tool(gitea).run(call())
    assert result == ToolResult("created issue 1: CI is red")
    assert gitea.issues[0]["body"] == f"test_x failed\n\n{MARKER}job-1:toolu_1"
    assert [r.method for r in gitea.requests] == ["POST"]


def test_the_result_names_the_issue_number_not_gitea_s_global_id(gitea: Gitea) -> None:
    gitea.add("someone else's", author="person")
    assert tool(gitea).run(call()) == ToolResult("created issue 2: CI is red")


def test_the_check_finds_the_issue_and_gives_the_result_the_run_gave(gitea: Gitea) -> None:
    built = tool(gitea)
    created = built.run(call())
    assert built.check(call()) == created
    assert len(gitea.issues) == 1


def test_the_check_lists_the_token_s_user_s_issues_and_never_searches(gitea: Gitea) -> None:
    """Gitea's search goes through a full-text index that lags and does not match exactly."""
    gitea.login = "the-token-owner"
    tool(gitea).check(call())
    params = gitea.issue_requests()[0].url.params
    assert "q" not in params
    assert (params["state"], params["type"], params["created_by"]) == (
        "all",
        "issues",
        "the-token-owner",
    )


def test_the_check_answers_not_done_after_a_list_without_the_issue(gitea: Gitea) -> None:
    built = tool(gitea)
    built.run(call("another-job:toolu_1"))
    assert built.check(call()) is CheckOutcome.NOT_DONE


def test_a_marker_the_model_wrote_into_a_body_does_not_answer_for_that_call(
    gitea: Gitea,
) -> None:
    """The body is the model's input. Only the line the tool appends counts."""
    built = tool(gitea)
    built.run(call("job-2:toolu_9", body=f"see\n{MARKER}job-1:toolu_1"))
    built.run(call("job-3:toolu_9", body=f"see\u2028{MARKER}job-1:toolu_1\r\n"))
    assert built.check(call("job-1:toolu_1")) is CheckOutcome.NOT_DONE


def test_an_issue_by_another_user_is_not_listed_and_does_not_count(gitea: Gitea) -> None:
    gitea.add(marked("planted", "job-1:toolu_1"), author="person")
    assert tool(gitea).check(call()) is CheckOutcome.NOT_DONE


def test_another_author_in_the_list_is_unknown(gitea: Gitea) -> None:
    """A list asked for one user's issues that holds another's cannot be trusted."""
    issue = {"number": 1, "title": "t", "body": marked("b", "job-1:toolu_1"),
             "user": {"login": "person"}}  # fmt: skip
    gitea.answer = httpx2.Response(200, json=[issue], headers={"X-Total-Count": "1"})
    assert tool(gitea).check(call()) is CheckOutcome.UNKNOWN


def test_the_user_name_is_matched_without_case_as_gitea_matches_it(gitea: Gitea) -> None:
    gitea.add(marked("b", "job-1:toolu_1"), author="lab-bot")
    gitea.login = "Lab-Bot"
    # The number is the tracker's, the title is the call's own.
    assert tool(gitea).check(call()) == ToolResult("created issue 1: CI is red")


def test_the_check_reads_every_page(gitea: Gitea) -> None:
    gitea.add(marked("b", "job-1:toolu_1"), title="old one")
    fill(gitea, 2 * PAGE + 10)
    # Issue 1 is the oldest, on the last page. Its stored title is not repeated.
    assert tool(gitea).check(call()) == ToolResult("created issue 1: CI is red")
    assert [int(r.url.params["page"]) for r in gitea.issue_requests()] == [1, 2, 3]


def test_a_full_last_page_is_followed_by_an_empty_one(gitea: Gitea) -> None:
    fill(gitea, PAGE)
    assert tool(gitea).check(call()) is CheckOutcome.NOT_DONE
    assert len(gitea.issue_requests()) == 2


def test_more_issues_than_the_page_cap_is_unknown(gitea: Gitea) -> None:
    fill(gitea, MAX_PAGES * PAGE + 1)
    assert tool(gitea).check(call()) is CheckOutcome.UNKNOWN
    assert len(gitea.issue_requests()) == MAX_PAGES


def test_an_issue_deleted_during_the_list_is_unknown_not_a_skipped_issue(gitea: Gitea) -> None:
    """The issue is first on page 2. A deletion on page 1 moves it up past the reader."""
    fill(gitea, PAGE - 1)
    gitea.add(marked("b", "job-1:toolu_1"))
    fill(gitea, PAGE)

    def delete_one(page: int) -> None:
        if page == 1:
            gitea.issues.pop()

    gitea.after_page = delete_one
    assert tool(gitea).check(call()) is CheckOutcome.UNKNOWN


@pytest.mark.parametrize("offset", [1, -1], ids=["more-than-seen", "fewer-than-seen"])
def test_a_count_that_does_not_match_the_issues_seen_is_unknown(gitea: Gitea, offset: int) -> None:
    fill(gitea, 3)
    gitea.count_offset = offset
    assert tool(gitea).check(call()) is CheckOutcome.UNKNOWN


def test_a_page_capped_lower_than_asked_is_unknown(gitea: Gitea) -> None:
    """A server that gives 30 per page looks like a last page with issues missing."""
    fill(gitea, 40)
    first_30 = list(reversed(gitea.issues))[:30]
    gitea.page_answer[1] = httpx2.Response(200, json=first_30, headers={"X-Total-Count": "40"})
    assert tool(gitea).check(call()) is CheckOutcome.UNKNOWN


GOOD = {"number": 1, "title": "t", "body": "b", "user": {"login": BOT}}


def listed(entries: Any, count: str | None = "1") -> httpx2.Response:
    headers = {} if count is None else {"X-Total-Count": count}
    return httpx2.Response(200, json=entries, headers=headers)


@pytest.mark.parametrize(
    "answer",
    [
        httpx2.ConnectError("refused"),
        httpx2.ReadTimeout("slow"),
        httpx2.Response(404, json={"message": "repository does not exist"}),
        httpx2.Response(500, text="boom"),
        httpx2.Response(200, text="not json", headers={"X-Total-Count": "0"}),
        listed({"not": "a list"}, "0"),
        listed([], None),
        listed([], "many"),
        listed([], "-1"),
        listed([], "+0"),
        listed([{"title": "t"}]),
        listed([{**GOOD, "body": None}]),
        listed([{**GOOD, "user": None}]),
        listed([{**GOOD, "user": {"login": 5}}]),
        listed(["not an issue"]),
        listed([{**GOOD, "body": marked("b", "job-1:toolu_1"), "number": "x"}]),
        httpx2.Response(200, content=b'[{"number": 1e400}]', headers={"X-Total-Count": "1"}),
    ],
    ids=["connect-error", "timeout", "404", "500", "not-json", "not-a-list", "no-count",
         "count-not-a-number", "count-negative", "count-signed", "no-body", "body-not-text",
         "no-user", "login-not-text", "entry-not-an-object", "found-with-bad-number",
         "number-overflows"],
)  # fmt: skip
def test_the_check_answers_unknown_without_a_clear_answer(gitea: Gitea, answer: Any) -> None:
    gitea.add(marked("b", "job-1:toolu_1"))
    gitea.answer = answer
    assert tool(gitea).check(call()) is CheckOutcome.UNKNOWN


@pytest.mark.parametrize(
    "answer",
    [httpx2.Response(401, json={"message": "token is required"}),
     httpx2.Response(403, json={"message": "token does not have scope read:user"}),
     httpx2.Response(200, json={"login": ""}),
     httpx2.Response(200, json={"id": 1})],
    ids=["401", "403-no-scope", "empty-name", "no-name"],
)  # fmt: skip
def test_a_token_whose_user_cannot_be_read_gives_unknown(
    gitea: Gitea, answer: httpx2.Response
) -> None:
    gitea.add(marked("b", "job-1:toolu_1"))
    gitea.user_answer = answer
    assert tool(gitea).check(call()) is CheckOutcome.UNKNOWN
    assert gitea.issue_requests() == []


def test_an_issue_gitea_stored_without_answering_is_reported_as_created(gitea: Gitea) -> None:
    """Told that the call failed, the model may make it again: a new call, a second issue."""

    def store_then_lose_the_answer(request: httpx2.Request) -> httpx2.Response:
        answer = gitea(request)
        if request.method == "POST":
            raise httpx2.ReadTimeout("slow")
        return answer

    lost = create_issue_tool(
        GiteaTracker(URL, REPO, TOKEN), transport=httpx2.MockTransport(store_then_lose_the_answer)
    )
    assert lost.run(call()) == ToolResult("created issue 1: CI is red")
    assert len(gitea.issues) == 1


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (f"x\n\n{MARKER}job-1:toolu_1", True),
        (f"x\n\n{MARKER}job-1:toolu_1\n", True),
        (f"x\r\n\r\n{MARKER}job-1:toolu_1", True),
        (f"{MARKER}job-1:toolu_1", True),
        (f"{MARKER}job-1:toolu_1\nmore", False),
        (f"x\n\n{MARKER}job-1:toolu_12", False),
        (f"x\n\nsee {MARKER}job-1:toolu_1", False),
        (f"x\n\n{MARKER}job-1:toolu_1 and more", False),
        ("", False),
    ],
)
def test_only_a_body_ending_in_the_marker_line_marks_a_call(body: str, expected: bool) -> None:
    assert marks(body, "job-1:toolu_1") is expected


def test_the_token_is_not_in_the_tracker_s_repr() -> None:
    assert TOKEN not in repr(GiteaTracker(URL, REPO, TOKEN))
