"""A secret the worker holds reaches no step, no log line and no API answer (issue 169).

One job runs with a marker as the provider's key and another as the tracker's
token, both also in the process environment. The job prints its tool
container's environment, opens an issue the tracker refuses, and ends on a
model call that fails. Then the job's steps and row, every log record the
process wrote, and every answer of the API about the job are searched for
both markers.

A failure names the place and the secret, never the text searched: that text
holds an environment dump, and a test log can be public.
"""

import dataclasses
import io
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi.routing import APIRoute

from runtime.api.app import create_app
from runtime.config import ApiConfig
from runtime.logs import configure_logging
from runtime.sandbox.runner import SandboxSettings
from runtime.store.steps import StepKind
from runtime.tools.base import ToolRegistry
from runtime.tools.create_issue import create_issue_tool
from runtime.tools.run_shell import run_shell_tool
from runtime.tools.trackers import GiteaTracker

from .conftest import Ran, Stack
from .test_api import Api, api_test
from .test_sandbox import OwnClientSandbox, settings, workspace_volume  # noqa: F401 - fixtures

# Letters and digits only: JSON and HTML escaping leave them as they are, so a
# plain search finds them in any answer. The token's form is what the worker
# accepts for TRACKER_TOKEN.
KEY = "canaryKey7Qm2Xv9"
TOKEN = "canaryToken4Rt8Wz1"  # noqa: S105 - a marker, not a secret
SECRETS = {"the provider key": KEY, "the tracker token": TOKEN}

# Every route of the API. A new route fails the test until it is searched too.
ROUTES = {
    ("GET", "/healthz"),
    ("GET", "/"),
    ("GET", "/jobs"),
    ("POST", "/jobs"),
    ("GET", "/jobs/{job_id}"),
    ("GET", "/jobs/{job_id}/steps"),
    ("GET", "/jobs/{job_id}/events"),
    ("POST", "/jobs/{job_id}/approve"),
    ("POST", "/jobs/{job_id}/deny"),
    ("POST", "/jobs/{job_id}/cancel"),
}


def leaks(place: str, text: str) -> list[str]:
    """`place: secret` for each marker in the text. Never the text itself."""
    return [f"{place}: {name}" for name, marker in SECRETS.items() if marker in text]


@contextmanager
def every_record() -> Iterator[io.StringIO]:
    """Every log record of this process, as `configure_logging` writes it in the worker and the API.

    A handler on the root logger sees what reaches stderr, whatever pytest
    does with stderr.
    """
    root, httpx2_logger = logging.getLogger(), logging.getLogger("httpx2")
    handlers, level, httpx2_level = root.handlers[:], root.level, httpx2_logger.level
    configure_logging()
    written = io.StringIO()
    try:
        handler = root.handlers[0]
        assert isinstance(handler, logging.StreamHandler)
        handler.setStream(written)
        yield written
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)
        httpx2_logger.setLevel(httpx2_level)


def api_routes() -> set[tuple[str, str]]:
    config = ApiConfig(
        database_url="postgresql://unused@127.0.0.1/unused",
        default_token_budget=1,
        default_timeout_seconds=1,
        host="127.0.0.1",
        port=0,
    )
    return {
        (method, route.path)
        for route in create_app(config).routes
        if isinstance(route, APIRoute)
        for method in route.methods or ()
    }


@dataclass(frozen=True)
class Answer:
    status: int
    text: str


async def every_answer(api: Api, job_id: str) -> dict[str, Answer]:
    """The answer of each route about the job, by method and path.

    `POST /jobs` is left out: it answers with a new job made from the request.
    The answers to approve, deny and cancel are refusals, as the job is final.
    """
    client = api.client
    answers: dict[str, Answer] = {}

    def keep(route: str, response: httpx2.Response) -> None:
        answers[route] = Answer(response.status_code, response.text)

    for path in ("/healthz", "/", "/?status=failed", "/jobs", "/jobs?status=failed"):
        keep(f"GET {path}", await client.get(path))
    for tail in ("", "/steps", "/events"):
        keep(f"GET /jobs/{{id}}{tail}", await client.get(f"/jobs/{job_id}{tail}"))
    for verb in ("approve", "deny"):
        keep(f"POST {verb}", await client.post(f"/jobs/{job_id}/{verb}", json={"question": 1}))
    keep("POST cancel", await client.post(f"/jobs/{job_id}/cancel"))
    return answers


# What each answer must be to count as searched: its status, and whether it
# holds the job's id and the environment dump.
EXPECTED = {
    "GET /healthz": (200, False, False),
    "GET /": (200, True, False),
    "GET /?status=failed": (200, True, False),
    "GET /jobs": (200, True, False),
    "GET /jobs?status=failed": (200, True, False),
    "GET /jobs/{id}": (200, True, False),
    # A step does not repeat its job's id.
    "GET /jobs/{id}/steps": (200, False, True),
    "GET /jobs/{id}/events": (200, False, True),
    "POST approve": (409, False, False),
    "POST deny": (409, False, False),
    "POST cancel": (409, False, False),
}


def records(written: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in written.splitlines()]


@pytest.fixture
def secrets_in_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker's environment holds both, as in the stack: a tool container must not get it."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.setenv("TRACKER_TOKEN", TOKEN)


def run_the_job(stack: Stack, sandbox: SandboxSettings, sent: list[str], keys: list[str]) -> Ran:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        sent.append(request.headers.get("Authorization", ""))
        return httpx2.Response(403, json={"message": "token is required"})

    async def to_the_provider(request: httpx2.Request) -> None:
        # The SDK sends the key as a bearer token.
        keys.append(request.headers.get("Authorization", ""))

    tracker = GiteaTracker("http://tracker.invalid", "owner/repo", TOKEN)
    tools = ToolRegistry(
        [
            run_shell_tool(OwnClientSandbox(sandbox)),
            create_issue_tool(tracker, transport=httpx2.MockTransport(refuse)),
        ]
    )
    spec = {"prompt": "Print the environment, then open an issue.", "model": "secret-canary"}
    return stack.run_one(spec, tools, api_key=KEY, on_request=to_the_provider)


@pytest.mark.usefixtures("secrets_in_the_environment")
def test_no_secret_reaches_a_step_a_log_line_or_an_answer_of_the_api(
    stack: Stack,
    database_url: str,
    workspace_volume: tuple[Path, str],  # noqa: F811 - the fixture imported above
    settings: SandboxSettings,  # noqa: F811 - the fixture imported above
) -> None:
    assert stack.workspace_root == workspace_volume[0]
    assert api_routes() == ROUTES
    sent: list[str] = []
    keys: list[str] = []

    with every_record() as written:
        ran = run_the_job(stack, settings, sent, keys)
        answers = api_test(database_url, lambda api: every_answer(api, str(ran.job.id)))

    # Each secret was in play: the leg that could leak it ran. Plain values
    # only in the assertions: pytest prints what an assertion compares, and
    # `ran` holds the environment dump.
    results = [step.payload for step in ran.steps if step.kind is StepKind.TOOL_RESULT]
    result_count, status, model_calls = len(results), ran.job.status.value, len(ran.requests)
    error = ran.job.error or ""
    assert result_count == 2
    environment, refused = results
    printed_an_environment = "PATH=" in environment["content"]
    assert printed_an_environment, "run_shell printed no environment"
    refused_with_403 = refused["is_error"] is True and refused["content"].startswith(
        "the tracker answered 403"
    )
    assert refused_with_403, "create_issue did not end in the tracker's 403"
    assert sent == [f"token {TOKEN}"]
    assert status == "failed"
    failed_on_the_model_call = error.startswith("model call failed: ")
    assert failed_on_the_model_call, "the job did not end on the failed model call"
    assert model_calls == 3
    assert keys == [f"Bearer {KEY}"] * 3
    # The worker's and the API's records went through the handler searched.
    loggers = {(entry["logger"], "job_id" in entry) for entry in records(written.getvalue())}
    assert ("runtime.worker", True) in loggers
    assert ("uvicorn.access", False) in loggers
    # Each answer is what it should be, and holds the job where it should.
    job_id = str(ran.job.id)
    shapes = {
        route: (answer.status, job_id in answer.text, "PATH=" in answer.text)
        for route, answer in answers.items()
    }
    assert shapes == EXPECTED

    found: list[str] = []
    for step in ran.steps:
        found += leaks(f"step {step.seq} ({step.kind.value})", json.dumps(step.payload))
    found += leaks("the job's row", json.dumps(dataclasses.asdict(ran.job), default=str))
    for number, line in enumerate(written.getvalue().splitlines(), 1):
        found += leaks(f"log line {number}", line)
    for route, answer in answers.items():
        found += leaks(route, answer.text)
    assert found == [], "a secret was found in:\n" + "\n".join(found)


def test_the_markers_are_found_where_they_are() -> None:
    """The search itself: a marker in a text is named, and the text is not."""
    assert leaks("here", f"x{KEY}y {TOKEN}") == [
        "here: the provider key",
        "here: the tracker token",
    ]
    assert leaks("here", "nothing") == []
