"""`GET /jobs` over real HTTP, against real Postgres."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx2
import pytest

from runtime.store.jobs import create_job
from runtime.store.status import JobStatus

from .test_api import Api, api_test, parked_for_approval

SUMMARY_FIELDS = {
    "id",
    "status",
    "token_budget",
    "tokens_used",
    "deadline",
    "error",
    "created_at",
    "question",
}


async def queued(api: Api, count: int) -> list[str]:
    """`count` queued jobs, oldest first, each with a spec the list must not return."""
    ids = []
    for _ in range(count):
        async with api.engine.begin() as conn:
            job = await create_job(
                conn,
                spec={"prompt": "a prompt nobody lists"},
                token_budget=1000,
                deadline=datetime.now(UTC) + timedelta(hours=1),
            )
        ids.append(str(job.id))
    return ids


async def jobs(api: Api, **params: Any) -> list[dict[str, Any]]:
    response = await api.client.get("/jobs", params=params)
    assert response.status_code == 200, response.text
    assert set(response.json()) == {"jobs"}
    found: list[dict[str, Any]] = response.json()["jobs"]
    return found


def test_no_jobs_is_an_empty_list(database_url: str) -> None:
    assert api_test(database_url, jobs) == []


def test_jobs_are_listed_newest_first_as_summaries(database_url: str) -> None:
    async def body(api: Api) -> tuple[list[str], httpx2.Response]:
        made = [str((await api.new_job()).id)]
        failed = await api.new_job()
        await api.finish(failed, JobStatus.FAILED)
        made.append(str(failed.id))
        made += await queued(api, 2)
        return made, await api.client.get("/jobs")

    made, response = api_test(database_url, body)

    found = response.json()["jobs"]
    assert [job["id"] for job in found] == made[::-1]
    assert [job["status"] for job in found] == ["queued", "queued", "failed", "running"]
    assert all(set(job) == SUMMARY_FIELDS for job in found)
    assert [job["error"] for job in found] == [None, None, "boom", None]
    assert all(job["question"] is None for job in found)
    assert "a prompt nobody lists" not in response.text


def test_a_waiting_job_is_listed_with_its_question_and_not_the_calls_input(
    database_url: str,
) -> None:
    async def body(api: Api) -> tuple[UUID, httpx2.Response]:
        job = await parked_for_approval(api)
        return job.id, await api.client.get("/jobs", params={"status": "needs_approval"})

    job_id, response = api_test(database_url, body)

    (found,) = response.json()["jobs"]
    assert found["id"] == str(job_id)
    assert found["question"] == {
        "seq": 2,
        "tool": "create_issue",
        "reason": "the tool's check could not tell whether the call happened",
    }
    # The input of the call the question asks about.
    assert "CI is red" not in response.text


def test_status_limit_and_before_select_the_page(database_url: str) -> None:
    async def body(api: Api) -> dict[str, list[str]]:
        waiting = [str((await parked_for_approval(api)).id) for _ in range(3)]
        rest = await queued(api, 4)
        everything = waiting + rest

        async def ids(**params: Any) -> list[str]:
            return [job["id"] for job in await jobs(api, **params)]

        first = await ids(limit=3)
        return {
            "made": everything,
            "first": first,
            "second": await ids(limit=3, before=first[-1]),
            "third": await ids(limit=3, before=everything[1]),
            "waiting": await ids(status="needs_approval"),
            "waiting, one": await ids(status="needs_approval", limit=1),
            "waiting, older": await ids(status="needs_approval", before=waiting[1]),
            "succeeded": await ids(status="succeeded"),
            "largest page": await ids(limit=200),
        }

    got = api_test(database_url, body)

    newest_first = got["made"][::-1]
    assert got["first"] == newest_first[:3]
    assert got["second"] == newest_first[3:6]
    assert got["third"] == newest_first[6:]
    assert got["waiting"] == newest_first[4:]
    assert got["waiting, one"] == newest_first[4:5]
    assert got["waiting, older"] == newest_first[6:]
    assert got["succeeded"] == []
    assert got["largest page"] == newest_first


def test_a_page_is_50_jobs_when_no_limit_is_given(database_url: str) -> None:
    async def body(api: Api) -> tuple[list[str], list[dict[str, Any]]]:
        return await queued(api, 51), await jobs(api)

    made, found = api_test(database_url, body)

    assert [job["id"] for job in found] == made[:0:-1]


@pytest.mark.parametrize(
    "params",
    [
        {"status": "done"},
        {"status": ""},
        {"status": "QUEUED"},
        {"limit": 0},
        {"limit": 201},
        {"limit": -1},
        {"limit": "1.5"},
        {"limit": "ten"},
        {"limit": 2**63},
        {"before": "not-a-job"},
        {"before": ""},
        {"before": "1"},
    ],
    ids=str,
)
def test_a_parameter_the_list_does_not_take_is_422(
    database_url: str, params: dict[str, Any]
) -> None:
    async def body(api: Api) -> httpx2.Response:
        await queued(api, 1)
        return await api.client.get("/jobs", params=params)

    response = api_test(database_url, body)

    assert response.status_code == 422
    assert "jobs" not in response.json()
    (name,) = params
    assert response.json()["detail"][0]["loc"] == ["query", name]
