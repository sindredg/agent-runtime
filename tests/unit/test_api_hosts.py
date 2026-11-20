"""The API answers only requests addressed to it by an allowed name.

A web page can point a name it controls at 127.0.0.1 (DNS rebinding). Its
requests then carry that name in `Host`. A page of another site can also
send a form to the API; its requests carry that site in `Origin`.
"""

import pytest
from fastapi.testclient import TestClient

from runtime.api.app import create_app
from runtime.config import ApiConfig, ConfigError

CONFIG = ApiConfig(
    database_url="postgresql+asyncpg://unused/unused",
    default_token_budget=1000,
    default_timeout_seconds=60,
    host="127.0.0.1",
    port=8000,
    allowed_hosts=("localhost", "127.0.0.1"),
)
JOB = "01a1d655-88bd-7468-afc4-5ca3a1947906"


def client(base_url: str = "http://127.0.0.1:8000") -> TestClient:
    # Not entered as a context manager: the lifespan, which connects to the
    # database, does not run. A refused request never reaches a route.
    return TestClient(create_app(CONFIG), base_url=base_url)


@pytest.mark.parametrize(
    "base_url", ["http://127.0.0.1:8000", "http://localhost:8000", "http://LOCALHOST"]
)
def test_an_allowed_host_is_answered(base_url: str) -> None:
    assert client(base_url).get("/healthz").status_code == 200


@pytest.mark.parametrize(
    "host",
    [
        "rebound.example",
        "rebound.example:8000",
        "127.0.0.1.rebound.example",
        "",
        "rebound.example@127.0.0.1",
        "localhost/x",
        "localhost:abc",
        "[::1]:8000",
    ],
)
def test_another_host_is_refused_on_every_route(host: str) -> None:
    api = client()
    for method, path in [("GET", "/healthz"), ("GET", f"/jobs/{JOB}"), ("POST", "/jobs")]:
        response = api.request(method, path, headers={"host": host}, json={})
        assert response.status_code == 400, (method, path, host)
        assert response.json() == {
            "detail": "the request's Host is not one this service answers to"
        }


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example",
        "null",
        "http://127.0.0.1.evil.example",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8000",
    ],
)
@pytest.mark.parametrize("path", ["/jobs", f"/jobs/{JOB}/cancel", f"/jobs/{JOB}/approve"])
def test_a_change_sent_from_another_sites_page_is_refused(origin: str, path: str) -> None:
    response = client().post(path, headers={"origin": origin}, json={"question": 1})
    assert response.status_code == 403
    assert response.json() == {"detail": "a change sent from another site's page is refused"}


def test_a_read_from_another_sites_page_is_not_refused_by_origin() -> None:
    # The browser keeps the answer from that page: no CORS header is sent.
    response = client().get("/healthz", headers={"origin": "https://evil.example"})
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_a_change_from_an_allowed_origin_or_none_reaches_the_route() -> None:
    # Validation runs in the route: a 422 shows the request got past the check.
    # The API's own pages (FastAPI's /docs) send its own origin.
    for headers in [{}, {"origin": "http://127.0.0.1:8000"}]:
        assert client().post("/jobs", headers=headers, json={}).status_code == 422


def test_the_allowed_hosts_come_from_the_environment() -> None:
    env = {"DATABASE_URL": "postgresql+asyncpg://x/y", "API_ALLOWED_HOSTS": " api.lab , LOCALHOST "}
    assert ApiConfig.from_env(env).allowed_hosts == ("api.lab", "localhost")
    assert ApiConfig.from_env({"DATABASE_URL": "x"}).allowed_hosts == ("localhost", "127.0.0.1")


@pytest.mark.parametrize("value", [",", "*", "evil.example:80"])
def test_a_list_that_would_allow_any_host_or_has_a_port_is_refused(value: str) -> None:
    with pytest.raises(ConfigError, match="API_ALLOWED_HOSTS"):
        ApiConfig.from_env({"DATABASE_URL": "x", "API_ALLOWED_HOSTS": value})


def test_a_request_with_two_hosts_is_refused() -> None:
    api = client()
    headers = [("host", "127.0.0.1"), ("host", "rebound.example")]
    response = api.get("/healthz", headers=headers)
    assert response.status_code == 400
