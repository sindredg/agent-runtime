"""The fake issue tracker in process: create issues, list them, clear them."""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from fake_tracker.app import MAX_BODY_LENGTH, MAX_ISSUES, create_app, first_issue

# The fake answers only the names the stack and the tests use.
LOCAL = "http://localhost"


def issue(**changes: Any) -> dict[str, Any]:
    return {
        "title": "CI is red",
        "body": "test_x failed",
        "operation_id": "job-1:toolu_1",
    } | changes


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(), base_url=LOCAL)


def test_a_created_issue_gets_a_number_and_is_listed(client: TestClient) -> None:
    created = client.post("/issues", json=issue())
    assert created.status_code == 201
    body = created.json()
    assert body["id"] == 1
    assert (body["title"], body["body"], body["operation_id"]) == (
        "CI is red",
        "test_x failed",
        "job-1:toolu_1",
    )
    assert body["created_at"]
    assert client.get("/issues").json() == [body]


def test_issue_numbers_rise_by_one(client: TestClient) -> None:
    ids = [client.post("/issues", json=issue(title=f"t{n}")).json()["id"] for n in range(3)]
    assert ids == [1, 2, 3]


def test_the_tracker_does_not_merge_two_issues_with_one_operation_id(client: TestClient) -> None:
    """It stores what it is sent. A caller that sends an issue twice shows up here as two."""
    client.post("/issues", json=issue())
    client.post("/issues", json=issue())
    listed = client.get("/issues", params={"operation_id": "job-1:toolu_1"}).json()
    assert [entry["id"] for entry in listed] == [1, 2]


def test_the_list_can_be_filtered_by_operation_id(client: TestClient) -> None:
    client.post("/issues", json=issue(operation_id="job-1:a"))
    client.post("/issues", json=issue(operation_id="job-2:a"))
    client.post("/issues", json=issue(operation_id="job-1:b"))
    assert [e["id"] for e in client.get("/issues", params={"operation_id": "job-2:a"}).json()] == [
        2
    ]
    assert client.get("/issues", params={"operation_id": "job-9:a"}).json() == []
    assert len(client.get("/issues").json()) == 3


@pytest.mark.parametrize(
    "body",
    [
        issue(title=""),
        issue(title=5),
        issue(title="x" * 201),
        issue(body=None),
        issue(body="x" * (MAX_BODY_LENGTH + 1)),
        issue(operation_id=""),
        issue(operation_id="x" * 201),
        issue(extra="field"),
        {"title": "no operation id", "body": ""},
        ["not", "an", "object"],
    ],
)
def test_an_issue_that_is_not_well_formed_is_refused_and_not_stored(
    client: TestClient, body: Any
) -> None:
    assert client.post("/issues", json=body).status_code == 422
    assert client.get("/issues").json() == []


def test_a_refusal_does_not_echo_the_input_back(client: TestClient) -> None:
    """Input that is not valid Unicode could not be encoded into the answer."""
    # A lone surrogate, written as a JSON escape, in a title that is too long.
    raw = '{"title": "' + "\\ud800 surrogate " * 50 + '", "body": "", "operation_id": "x"}'
    refused = client.post("/issues", content=raw, headers={"content-type": "application/json"})
    assert refused.status_code == 422
    assert "surrogate" not in refused.text


def test_issues_can_be_cleared(client: TestClient) -> None:
    client.post("/issues", json=issue())
    assert client.delete("/issues").status_code == 204
    assert client.get("/issues").json() == []
    # Numbering does not start over, so an id never names two issues.
    assert client.post("/issues", json=issue()).json()["id"] == 2


def test_a_full_tracker_refuses_new_issues(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fake_tracker.app.MAX_ISSUES", 2)
    client = TestClient(create_app(), base_url=LOCAL)
    assert MAX_ISSUES > 2
    assert [client.post("/issues", json=issue()).status_code for _ in range(3)] == [201, 201, 507]
    assert len(client.get("/issues").json()) == 2


def test_healthz(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


# The first issue number


def test_without_a_first_number_the_tracker_numbers_from_one() -> None:
    for env in ({}, {"FAKE_TRACKER_FIRST_ISSUE": ""}):
        assert first_issue(env) == 1
        created = TestClient(create_app(env), base_url=LOCAL).post("/issues", json=issue())
        assert created.json()["id"] == 1


def test_the_long_stacks_first_number_keeps_one_width_to_the_last() -> None:
    env = {"FAKE_TRACKER_FIRST_ISSUE": "100000"}
    client = TestClient(create_app(env), base_url=LOCAL)

    ids = [client.post("/issues", json=issue()).json()["id"] for _ in range(3)]

    assert ids == [100_000, 100_001, 100_002]
    assert len(str(first_issue(env) + MAX_ISSUES - 1)) == 6


def test_every_number_of_a_full_tracker_has_the_width_of_the_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """From the first to the MAX_ISSUES-th number, with a small tracker so it can be filled."""
    monkeypatch.setattr("fake_tracker.app.MAX_ISSUES", 90)
    client = TestClient(create_app({"FAKE_TRACKER_FIRST_ISSUE": "10"}), base_url=LOCAL)

    answers = [client.post("/issues", json=issue()) for _ in range(91)]

    ids = [answer.json()["id"] for answer in answers[:90]]
    assert ids == list(range(10, 100))
    assert {len(str(number)) for number in ids} == {2}
    assert answers[90].status_code == 507


@pytest.mark.parametrize("first", ["999999", "1", "99999", "910000"])
def test_a_first_number_whose_width_would_change_stops_the_tracker(first: str) -> None:
    """The last number a full tracker gives, first + MAX_ISSUES - 1, has more digits."""
    with pytest.raises(ValueError, match="would not have one width"):
        create_app({"FAKE_TRACKER_FIRST_ISSUE": first})


# The last: 100000 in Arabic-Indic digits, which `str.isdigit` accepts.
ARABIC_INDIC = "\u0661\u0660\u0660\u0660\u0660\u0660"


@pytest.mark.parametrize("first", ["0", "-5", "abc", "1e5", " 100000", "100000.0", ARABIC_INDIC])
def test_a_first_number_that_is_not_a_positive_whole_number_stops_the_tracker(first: str) -> None:
    with pytest.raises(ValueError, match="positive whole number"):
        create_app({"FAKE_TRACKER_FIRST_ISSUE": first})
