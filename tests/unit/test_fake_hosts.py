"""The fakes answer only the names the stack and the tests use, as the API does.

A web page that points a name it controls at 127.0.0.1 could otherwise set
the fake provider busy, read its request log, or clear and fill the fake
tracker, whose list the `create_issue` check reads, or read and clear the
fake receiver's log of notifications.
"""

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fake_provider.app import create_app as create_provider
from fake_receiver.app import create_app as create_receiver
from fake_tracker.app import create_app as create_tracker

FOREIGN = "rebound.example:8001"
BUSY = {"seconds": 3600, "status": 429, "retry_after": 3600}
ISSUE = {"title": "t", "body": "b", "operation_id": "op-1"}
HEADERS = {"x-api-key": "test", "anthropic-version": "2026-10-21"}
# The requests of issue 132's table, each with a foreign Host and an Origin
# that matches it, the way a rebound page sends them.
PROVIDER_REQUESTS: list[tuple[str, str, Any]] = [
    ("PUT", "/_fake/busy", BUSY),
    ("GET", "/_fake/requests", None),
    ("DELETE", "/_fake/busy", None),
    ("POST", "/v1/messages", {"model": "two", "max_tokens": 1, "messages": []}),
    ("GET", "/healthz", None),
]
TRACKER_REQUESTS: list[tuple[str, str, Any]] = [
    ("GET", "/issues", None),
    ("DELETE", "/issues", None),
    ("POST", "/issues", ISSUE),
    ("GET", "/healthz", None),
]
RECEIVER_REQUESTS: list[tuple[str, str, Any]] = [
    ("GET", "/notifications", None),
    ("DELETE", "/notifications", None),
    ("POST", "/notifications", {"id": "j:1"}),
    ("POST", "/_fake/answers", {"answers": [{"status": 500}]}),
    ("GET", "/healthz", None),
]
CASES = (
    [("fake-provider", create_provider, *r) for r in PROVIDER_REQUESTS]
    + [("fake-tracker", lambda: create_tracker({}), *r) for r in TRACKER_REQUESTS]
    + [("fake-receiver", create_receiver, *r) for r in RECEIVER_REQUESTS]
)


@pytest.mark.parametrize(("name", "create", "method", "path", "body"), CASES)
def test_a_foreign_host_is_refused(
    name: str, create: Any, method: str, path: str, body: Any
) -> None:
    client = TestClient(create(), base_url=f"http://{FOREIGN}")
    response = client.request(method, path, json=body, headers={"origin": f"http://{FOREIGN}"})
    assert response.status_code == 400, (name, method, path)


def test_a_refused_request_changes_nothing() -> None:
    tracker = create_tracker({})
    TestClient(tracker, base_url=f"http://{FOREIGN}").post("/issues", json=ISSUE)
    assert TestClient(tracker, base_url="http://localhost").get("/issues").json() == []
    provider = create_provider()
    message = {"model": "hello", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]}
    foreign = TestClient(provider, base_url=f"http://{FOREIGN}")
    foreign.put("/_fake/busy", json=BUSY)
    foreign.post("/v1/messages", json=message, headers=HEADERS)
    local = TestClient(provider, base_url="http://127.0.0.1:8001")
    # The refused message was not logged, and the busy window was not set.
    assert local.get("/_fake/requests").json() == []
    assert local.post("/v1/messages", json=message, headers=HEADERS).status_code == 200


@pytest.mark.parametrize(
    ("create", "path"),
    [
        (create_provider, "/_fake/busy"),
        (lambda: create_tracker({}), "/issues"),
        (create_receiver, "/notifications"),
    ],
)
@pytest.mark.parametrize("origin", ["https://evil.example", "null", "http://localhost:3000"])
def test_a_change_from_another_sites_page_is_refused(create: Any, path: str, origin: str) -> None:
    client = TestClient(create(), base_url="http://localhost:8001")
    response = client.delete(path, headers={"origin": origin})
    assert response.status_code == 403


@pytest.mark.parametrize(
    ("app", "own_name", "other_fake"),
    [
        (create_provider(), "fake-provider", "fake-tracker"),
        (create_tracker({}), "fake-tracker", "fake-provider"),
        (create_receiver(), "fake-receiver", "fake-tracker"),
    ],
)
def test_each_fake_answers_its_own_name_and_localhost_only(
    app: FastAPI, own_name: str, other_fake: str
) -> None:
    for host in [own_name, f"{own_name}:8000", "127.0.0.1:8001", "localhost", "LOCALHOST:8002"]:
        assert TestClient(app, base_url=f"http://{host}").get("/healthz").status_code == 200, host
    for host in [other_fake, "testserver", "api", "gitea", "[::1]:8001"]:
        assert TestClient(app, base_url=f"http://{host}").get("/healthz").status_code == 400, host
