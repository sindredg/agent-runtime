"""The status page (`GET /`) over real HTTP, against real Postgres: a job in
every status, with hostile text where a job carries text."""

from typing import Any
from uuid import UUID

from runtime.api import page
from runtime.store import held
from runtime.store.jobs import cancel_job
from runtime.store.status import JobStatus
from runtime.store.steps import StepKind

from .test_api import Api, api_test
from .test_api_list import queued

# A tag, an attribute that breaks out of its quotes, and the end of the style.
HOSTILE = '<script>alert(1)</script>" onmouseover="alert(1)</style>'


async def every_status(api: Api) -> dict[JobStatus, str]:
    """One job in each status, made the way the API and a worker make it.

    `new_job` claims the oldest queued job, so the queued ones come last.
    """
    made: dict[JobStatus, str] = {}
    for status in (JobStatus.WAITING, JobStatus.SUCCEEDED):
        job = await api.new_job()
        await api.finish(job, status)
        made[status] = str(job.id)
    job = await api.new_job()
    await held.fail(api.engine, job_id=job.id, lease_epoch=job.lease_epoch, error=HOSTILE)
    made[JobStatus.FAILED] = str(job.id)
    job = await api.new_job()
    asked = {
        "tool_use_id": "toolu_1",
        "name": HOSTILE,
        "input": {},
        "operation_id": f"{job.id}:toolu_1",
        "reason": HOSTILE,
    }
    await held.append_step(
        api.engine,
        job_id=job.id,
        lease_epoch=1,
        seq=1,
        kind=StepKind.APPROVAL_REQUESTED,
        payload=asked,
    )
    await held.leave_running(
        api.engine,
        job_id=job.id,
        lease_epoch=1,
        new_status=JobStatus.NEEDS_APPROVAL,
        question=held.Question(1, held.QuestionReason.BEFORE_RUN),
    )
    made[JobStatus.NEEDS_APPROVAL] = str(job.id)
    made[JobStatus.RUNNING] = str((await api.new_job()).id)
    made[JobStatus.QUEUED], to_cancel = await queued(api, 2)
    async with api.engine.begin() as conn:
        await cancel_job(conn, UUID(to_cancel))
    made[JobStatus.CANCELLED] = to_cancel
    for status, job_id in made.items():
        assert (await api.client.get(f"/jobs/{job_id}")).json()["status"] == status.value
    return made


def test_the_page_lists_a_job_in_every_status_and_escapes_their_text(
    database_url: str,
) -> None:
    async def body(api: Api) -> tuple[dict[JobStatus, str], Any]:
        made = await every_status(api)
        return made, await api.client.get("/")

    made, response = api_test(database_url, body)

    assert set(made) == set(JobStatus)
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    for name, value in page.HEADERS.items():
        assert response.headers[name] == value
    for job_id in made.values():
        assert f'href="/jobs/{job_id}/steps"' in response.text
    assert HOSTILE not in response.text
    assert "&lt;script&gt;" in response.text
    assert "<script" not in response.text


def test_the_page_takes_the_lists_filter_and_limit(database_url: str) -> None:
    async def body(api: Api) -> tuple[list[str], Any, Any]:
        job = await api.new_job()
        await api.finish(job, JobStatus.SUCCEEDED)
        ids = await queued(api, 3)
        return (
            ids,
            await api.client.get("/", params={"status": "queued", "limit": 2}),
            await api.client.get("/", params={"status": "nonsense"}),
        )

    ids, response, refused = api_test(database_url, body)

    assert response.status_code == 200
    # Newest first: the two newest queued jobs, and a link to the older ones.
    assert ids[2] in response.text and ids[1] in response.text
    assert f'href="/jobs/{ids[0]}/steps"' not in response.text
    assert f"/?status=queued&amp;before={ids[1]}&amp;limit=2" in response.text
    assert "succeeded</td>" not in response.text
    assert refused.status_code == 422
