"""The fake issue tracker in process: create issues, list them, clear them."""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from fake_tracker.app import MAX_BODY_LENGTH, MAX_ISSUES, create_app


def issue(**changes: Any) -> dict[str, Any]:
    return {
        "title": "CI is red",
        "body": "test_x failed",
        "operation_id": "job-1:toolu_1",
    } | changes


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


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
    client = TestClient(create_app())
    assert MAX_ISSUES > 2
    assert [client.post("/issues", json=issue()).status_code for _ in range(3)] == [201, 201, 507]
    assert len(client.get("/issues").json()) == 2


def test_healthz(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
