"""The notification's POST and its setting (ADR 0029), without a database.

The POST goes to the configured address and nowhere else: no redirect is
followed and no proxy from the environment is used. One deadline covers the
whole request, and a stop gives it up.
"""

import asyncio
import time
from typing import Any
from uuid import UUID

import httpx2
import pytest

from fake_receiver.app import create_app
from fake_receiver.serve import serving
from runtime.config import ConfigError, WorkerConfig
from runtime.store.held import QuestionReason
from runtime.store.notify import Due, retry_in
from runtime.worker import notify
from runtime.worker.notify import body, post

WORKER_ENV = {"DATABASE_URL": "postgresql://u:p@h/db", "ANTHROPIC_API_KEY": "k", "MODEL": "m"}
JOB = UUID("01890000-0000-7000-8000-000000000001")
DUE = Due(JOB, 3, QuestionReason.BEFORE_RUN, 1, "create_issue")


def received(url: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = httpx2.get(f"{url}/notifications").json()
    return found


# --- the setting


def test_notify_url_unset_means_no_notifications() -> None:
    assert WorkerConfig.from_env(WORKER_ENV).notify_url is None


@pytest.mark.parametrize(
    "value",
    [
        "http://receiver:8000/notifications",
        "https://hooks.example/T0/B1/abc/",
        "http://receiver/hook?token=abc",
        "http://10.0.0.5:9000",
    ],
)
def test_notify_url_is_kept_exactly_as_given(value: str) -> None:
    """The POST goes to that address: no slash trimmed, the query kept."""
    assert WorkerConfig.from_env({**WORKER_ENV, "NOTIFY_URL": value}).notify_url == value


@pytest.mark.parametrize(
    "value",
    [
        "receiver:8000",
        "ftp://receiver/pw",
        "file:///etc/pw",
        "http://",
        "http:///pw",
        "http://user:pw@receiver",
        "http://pw@receiver",
        "http:user:pw@receiver",
        "http://receiver/hook#pw",
        "http://receiver:port/pw",
        "http://[::1/pw",
        "http://receiver/a pw",
        "http://receiver/\npw",
    ],
)
def test_a_notify_url_that_cannot_be_used_is_refused_without_repeating_it(value: str) -> None:
    with pytest.raises(ConfigError, match="NOTIFY_URL") as raised:
        WorkerConfig.from_env({**WORKER_ENV, "NOTIFY_URL": value})
    assert "pw" not in str(raised.value)


def test_notify_url_is_not_in_the_settings_repr() -> None:
    config = WorkerConfig.from_env({**WORKER_ENV, "NOTIFY_URL": "http://r/hook?token=secret1"})
    assert "secret1" not in repr(config)


# --- what is sent


def test_the_body_names_job_question_tool_and_reason_and_nothing_else() -> None:
    assert body(DUE) == {
        "id": f"{JOB}:3",
        "job_id": str(JOB),
        "question": 3,
        "tool": "create_issue",
        "reason": "before_run",
    }


def test_tries_after_a_failure_wait_2_4_8_16_then_30_seconds() -> None:
    waits = [retry_in(attempts).total_seconds() for attempts in range(1, 8)]
    assert waits == [2, 4, 8, 16, 30, 30, 30]


# --- the POST


def test_a_post_reaches_the_receiver_with_the_body() -> None:
    async def run() -> tuple[int, list[dict[str, Any]]]:
        async with serving(create_app()) as url:
            status = await post(f"{url}/notifications", DUE)
            return status, await asyncio.to_thread(received, url)

    status, arrivals = asyncio.run(run())
    assert status == 200
    assert [a["body"] for a in arrivals] == [body(DUE)]
    assert arrivals[0]["content_type"] == "application/json"


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_redirect_is_not_followed(status: int) -> None:
    """The address called is the configured one. A redirect is a failure, not a hop."""

    async def run() -> tuple[int, list[dict[str, Any]]]:
        async with serving(create_app()) as url:
            await asyncio.to_thread(
                httpx2.post, f"{url}/_fake/answers", json={"answers": [{"status": status}]}
            )
            answered = await post(f"{url}/notifications", DUE)
            return answered, await asyncio.to_thread(received, url)

    answered, arrivals = asyncio.run(run())
    assert answered == status
    assert len(arrivals) == 1


def test_a_proxy_named_in_the_environment_is_not_used(monkeypatch: pytest.MonkeyPatch) -> None:
    """Through a proxy, the POST would go elsewhere first. Port 9 of 127.0.0.1 answers nothing."""
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    async def run() -> int:
        async with serving(create_app()) as url:
            return await post(f"{url}/notifications", DUE)

    assert asyncio.run(run()) == 200


def test_one_deadline_covers_the_whole_post(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify, "POST_WITHIN", 0.5)

    async def run() -> float:
        async with serving(create_app()) as url:
            await asyncio.to_thread(
                httpx2.post, f"{url}/_fake/answers", json={"answers": [{"hang": 5}]}
            )
            started = time.monotonic()
            with pytest.raises(TimeoutError):
                await post(f"{url}/notifications", DUE)
            return time.monotonic() - started

    assert asyncio.run(run()) < 2


def test_a_stop_gives_up_a_post_in_flight() -> None:
    """A worker told to stop does not wait for the receiver: the row comes due again."""

    async def run() -> tuple[int | None, float]:
        stop = asyncio.Event()
        async with serving(create_app()) as url:
            await asyncio.to_thread(
                httpx2.post, f"{url}/_fake/answers", json={"answers": [{"hang": 5}]}
            )
            work = asyncio.create_task(post(f"{url}/notifications", DUE))
            asyncio.get_running_loop().call_later(0.3, stop.set)
            started = time.monotonic()
            result = await notify._unless_stopped(stop, work)
            return result, time.monotonic() - started

    result, took = asyncio.run(run())
    assert result is None
    assert took < 2
