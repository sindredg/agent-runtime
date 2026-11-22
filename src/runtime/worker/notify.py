"""Post each parked job's question to the operator's receiver (ADR 0029).

Every worker with NOTIFY_URL set runs this loop. Each second it closes the
rows whose job no longer waits on them, then claims, posts and records due
rows one at a time until none is left. No transaction is open during a
POST. A failed POST, or a worker that stops during one, costs a repeat
later, never a question that is not sent: the promise is at least once.

The POST has one 10 s deadline, uses no proxy from the environment, and
follows no redirect. Any 2xx is a delivery; anything else is a failure. The
receiver's body is not read. The URL is never logged: a webhook's path or
query can hold a token.
"""

import asyncio
import contextlib
import logging
from typing import Any

import httpx2
from sqlalchemy.ext.asyncio import AsyncEngine

from runtime.store.notify import Due, claim_due, close_settled, record_delivered, record_failed
from runtime.tools.trackers import Network, given_up_at_once

log = logging.getLogger("runtime.worker")

# Seconds one POST may take, from the connect to the end of the answer.
POST_WITHIN = 10.0
# Seconds between two looks for due rows.
POLL_SECONDS = 1.0


def body(due: Due) -> dict[str, Any]:
    """The POST's JSON. No tool input and no reason text: both can hold what a target wrote."""
    return {
        "id": f"{due.job_id}:{due.question}",
        "job_id": str(due.job_id),
        "question": due.question,
        "tool": due.tool,
        "reason": due.reason.value,
    }


async def post(url: str, due: Due, transport: Network | None = None) -> int:
    """POST the question and return the status. Raises TimeoutError or httpx2.HTTPError."""
    async with (
        asyncio.timeout(POST_WITHIN),
        httpx2.AsyncClient(
            timeout=None, follow_redirects=False, transport=given_up_at_once(transport)
        ) as http,
    ):
        response = await http.post(url, json=body(due))
        return response.status_code


async def _unless_stopped(stop: asyncio.Event, work: asyncio.Task[int]) -> int | None:
    """The result of `work`, or None when `stop` was set first. `work` is then cancelled."""
    stopped = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait({work, stopped}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stopped.cancel()
    if not work.done():
        work.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await work
        return None
    return work.result()


async def deliver_due(
    engine: AsyncEngine, url: str, stop: asyncio.Event, transport: Network | None = None
) -> int:
    """Close the settled rows, then post every due row once. Returns how many were delivered."""
    async with engine.begin() as conn:
        await close_settled(conn)
    delivered = 0
    while not stop.is_set():
        async with engine.begin() as conn:
            due = await claim_due(conn)
        if due is None:
            break
        where = {"job_id": due.job_id, "question": due.question, "attempts": due.attempts}
        failure: str | None
        try:
            status = await _unless_stopped(stop, asyncio.create_task(post(url, due, transport)))
        except (TimeoutError, httpx2.HTTPError) as error:
            # The type only: an error's text can hold the URL.
            failure = type(error).__name__
        else:
            if status is None:
                # Stopped during the POST. The row comes due again after its push.
                break
            failure = None if 200 <= status < 300 else f"status {status}"
        if failure is None:
            async with engine.begin() as conn:
                logged = await record_delivered(conn, due)
            delivered += 1
            log.info("notification delivered", extra={**where, "logged": logged})
        else:
            async with engine.begin() as conn:
                await record_failed(conn, due)
            log.warning("notification not delivered", extra={**where, "failure": failure})
    return delivered


async def keep_notifying(
    engine: AsyncEngine,
    url: str,
    stop: asyncio.Event,
    interval: float = POLL_SECONDS,
    transport: Network | None = None,
) -> None:
    """Deliver due rows every `interval` seconds until `stop` is set. A failed round is logged."""
    while not stop.is_set():
        try:
            await deliver_due(engine, url, stop, transport)
        except Exception:
            log.exception("notification round failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            continue
