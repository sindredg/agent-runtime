"""The fake notification receiver in process: log POSTs, answer as queued, clear."""

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from fake_receiver.app import MAX_BODY_BYTES, MAX_QUEUED, create_app

LOCAL = "http://localhost"
NOTE = {"id": "j:3", "job_id": "j", "question": 3, "tool": "create_issue", "reason": "before_run"}


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(), base_url=LOCAL, follow_redirects=False)


def arrivals(client: TestClient) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = client.get("/notifications").json()
    return found


def test_a_post_is_answered_200_and_logged_as_it_came(client: TestClient) -> None:
    response = client.post("/notifications", json=NOTE)
    assert response.status_code == 200
    [arrival] = arrivals(client)
    assert arrival["body"] == NOTE
    assert arrival["status"] == 200
    assert arrival["content_type"] == "application/json"
    assert arrival["received_at"]


def test_repeats_are_kept_as_two_arrivals(client: TestClient) -> None:
    """The receiver does not drop a repeat: the log shows what the runtime sent."""
    client.post("/notifications", json=NOTE)
    client.post("/notifications", json=NOTE)
    assert [a["body"]["id"] for a in arrivals(client)] == ["j:3", "j:3"]


def test_queued_answers_are_used_in_order_then_200_again(client: TestClient) -> None:
    queued = {"answers": [{"status": 503}, {"status": 404}]}
    assert client.post("/_fake/answers", json=queued).status_code == 204
    statuses = [client.post("/notifications", json=NOTE).status_code for _ in range(3)]
    assert statuses == [503, 404, 200]
    assert [a["status"] for a in arrivals(client)] == [503, 404, 200]


def test_a_queued_redirect_points_back_here(client: TestClient) -> None:
    """A sender that follows it shows up as a second arrival."""
    client.post("/_fake/answers", json={"answers": [{"status": 307}]})
    response = client.post("/notifications", json=NOTE)
    assert response.status_code == 307
    assert response.headers["location"] == "/notifications"


def test_a_queued_hang_waits_before_answering(client: TestClient) -> None:
    client.post("/_fake/answers", json={"answers": [{"hang": 0.3}]})
    started = time.monotonic()
    assert client.post("/notifications", json=NOTE).status_code == 200
    assert time.monotonic() - started >= 0.3
    assert arrivals(client)[0]["hang"] == 0.3


def test_a_body_that_is_not_json_or_too_long_is_logged_without_it(client: TestClient) -> None:
    client.post("/notifications", content=b"not json")
    client.post("/notifications", content=b"[" + b"1," * MAX_BODY_BYTES + b"1]")
    assert [a["body"] for a in arrivals(client)] == [None, None]


def test_delete_forgets_arrivals_and_queued_answers(client: TestClient) -> None:
    client.post("/_fake/answers", json={"answers": [{"status": 500}]})
    client.post("/notifications", json=NOTE)
    client.post("/_fake/answers", json={"answers": [{"status": 500}]})
    assert client.delete("/notifications").status_code == 204
    assert arrivals(client) == []
    assert client.post("/notifications", json=NOTE).status_code == 200


@pytest.mark.parametrize(
    "answer",
    [{"status": 199}, {"status": 600}, {"status": "500"}, {"hang": -1}, {"hang": 121}, {"x": 1}],
)
def test_an_answer_that_cannot_be_given_is_refused(client: TestClient, answer: Any) -> None:
    assert client.post("/_fake/answers", json={"answers": [answer]}).status_code == 422


def test_the_queue_is_bounded(client: TestClient) -> None:
    full = {"answers": [{"status": 500}] * MAX_QUEUED}
    assert client.post("/_fake/answers", json=full).status_code == 204
    assert client.post("/_fake/answers", json={"answers": [{}]}).status_code == 507


def test_a_queued_endless_body_keeps_coming() -> None:
    client = TestClient(create_app(), base_url=LOCAL)
    client.post("/_fake/answers", json={"answers": [{"endless_body": True}]})
    with client.stream("POST", "/notifications", json=NOTE) as response:
        assert response.status_code == 200
        read = 0
        for chunk in response.iter_bytes():
            read += len(chunk)
            if read > 1024 * 1024:
                break
    assert read > 1024 * 1024
