"""The store against a real Postgres: jobs, claiming, steps, status."""

import asyncio
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import event, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from runtime.store.jobs import (
    TAKEOVER_LIMIT,
    InvalidTransition,
    Job,
    claim_job,
    claim_statement,
    create_job,
    get_job,
    update_status,
)
from runtime.store.payload import InvalidPayload, check_json
from runtime.store.status import TRANSITIONS, JobStatus
from runtime.store.steps import StepConflict, StepKind, read_progress, read_steps

from .conftest import DEADLINE, add_step, expire_lease, in_transaction, new_job, with_engine

S = JobStatus


# Jobs


def test_created_job_is_queued_and_can_be_read_back(migrated_database_url: str) -> None:
    async def body(conn: AsyncConnection) -> tuple[Job, Job | None]:
        job = await new_job(conn)
        return job, await get_job(conn, job.id)

    created, read = in_transaction(migrated_database_url, body)
    assert read == created
    assert created.status is S.QUEUED
    assert created.spec == {"task": "hello"}
    assert created.token_budget == 1000
    assert created.deadline == DEADLINE
    assert created.lease_epoch == 0
    assert created.lease_owner is None


def test_get_job_returns_none_for_an_unknown_id(migrated_database_url: str) -> None:
    assert in_transaction(migrated_database_url, lambda c: get_job(c, uuid4())) is None


# Claiming


def test_claim_returns_none_when_nothing_is_queued(database_url: str) -> None:
    assert in_transaction(database_url, lambda c: claim_job(c, worker_id="w1")) is None


def test_claim_takes_the_lease(database_url: str) -> None:
    async def body(conn: AsyncConnection) -> tuple[Job, Job | None, datetime, datetime]:
        created = await new_job(conn)
        # Time passes inside the transaction before the claim. The lease must
        # run from the claim, not from the start of the transaction.
        await conn.execute(text("SELECT pg_sleep(0.5)"))
        before = (await conn.execute(text("SELECT clock_timestamp()"))).scalar_one()
        claimed = await claim_job(conn, worker_id="w1", lease=timedelta(seconds=30))
        after = (await conn.execute(text("SELECT clock_timestamp()"))).scalar_one()
        return created, claimed, before, after

    created, claimed, before, after = in_transaction(database_url, body)
    assert claimed is not None
    assert claimed.id == created.id
    assert claimed.status is S.RUNNING
    assert claimed.lease_owner == "w1"
    assert claimed.lease_epoch == 1
    assert claimed.lease_expires_at is not None
    lease = timedelta(seconds=30)
    assert before + lease <= claimed.lease_expires_at <= after + lease


@pytest.mark.parametrize("lease", [timedelta(0), timedelta(seconds=-1)])
def test_claim_refuses_a_lease_that_is_not_positive(database_url: str, lease: timedelta) -> None:
    async def body(conn: AsyncConnection) -> Job | None:
        await new_job(conn)
        return await claim_job(conn, worker_id="w1", lease=lease)

    with pytest.raises(ValueError, match="positive"):
        in_transaction(database_url, body)


def test_jobs_are_claimed_oldest_first_and_once(database_url: str) -> None:
    async def body(engine: AsyncEngine) -> tuple[list[UUID], list[UUID | None]]:
        created = []
        for _ in range(3):
            # One transaction per job, so each gets a later created_at.
            async with engine.begin() as conn:
                created.append((await new_job(conn)).id)
        claimed = []
        for _ in range(4):
            async with engine.begin() as conn:
                job = await claim_job(conn, worker_id="w1")
                claimed.append(None if job is None else job.id)
        return created, claimed

    created, claimed = with_engine(database_url, body)
    assert claimed == [*created, None]


def test_jobs_that_are_not_queued_and_hold_no_expired_lease_are_not_claimed(
    database_url: str,
) -> None:
    async def body(conn: AsyncConnection) -> Job | None:
        for status in JobStatus:
            if status is not S.QUEUED:
                job = await new_job(conn)
                await conn.execute(
                    text("UPDATE jobs SET status = :status WHERE id = :id"),
                    {"status": status, "id": job.id},
                )
        return await claim_job(conn, worker_id="w1")

    assert in_transaction(database_url, body) is None


def test_rolled_back_claim_leaves_the_job_queued(database_url: str) -> None:
    async def body(engine: AsyncEngine) -> Job | None:
        async with engine.begin() as conn:
            job = await new_job(conn)
        async with engine.connect() as conn:
            assert await claim_job(conn, worker_id="w1") is not None
            await conn.rollback()
        async with engine.begin() as conn:
            return await get_job(conn, job.id)

    job = with_engine(database_url, body)
    assert job is not None
    assert job.status is S.QUEUED
    assert job.lease_epoch == 0


def test_20_concurrent_claimers_on_10_jobs_claim_each_job_once(database_url: str) -> None:
    jobs_count, claimers = 10, 20

    async def body(engine: AsyncEngine) -> tuple[set[UUID], list[Job | None]]:
        async with engine.begin() as conn:
            created = {(await new_job(conn)).id for _ in range(jobs_count)}

        # Every claimer has its transaction open before the first claim runs.
        ready = asyncio.Barrier(claimers)

        async def claimer(n: int) -> Job | None:
            async with engine.connect() as conn, conn.begin():
                await conn.execute(text("SELECT 1"))
                await ready.wait()
                job = await claim_job(conn, worker_id=f"w{n}")
                # Hold the row lock, so the other claims run while it is held.
                await asyncio.sleep(0.2)
                return job

        async with asyncio.timeout(30):
            results = await asyncio.gather(*(claimer(n) for n in range(claimers)))
        return created, list(results)

    # One connection per claimer: 20 claims in flight at once.
    created, results = with_engine(database_url, body, connections=claimers)
    claimed = [job for job in results if job is not None]
    assert len(claimed) == jobs_count
    assert {job.id for job in claimed} == created
    assert len({job.lease_owner for job in claimed}) == jobs_count
    assert all(job.lease_epoch == 1 for job in claimed)

    # The database agrees with what the claimers were told.
    async def stored(conn: AsyncConnection) -> list[tuple[str, int, str]]:
        rows = await conn.execute(text("SELECT status, lease_epoch, lease_owner FROM jobs"))
        return [(row.status, row.lease_epoch, row.lease_owner) for row in rows]

    rows = in_transaction(database_url, stored)
    assert sorted(owner for _, _, owner in rows) == sorted(str(j.lease_owner) for j in claimed)
    assert all(status == "running" and epoch == 1 for status, epoch, _ in rows)


def test_claim_does_not_wait_for_a_job_another_worker_is_claiming(database_url: str) -> None:
    """SKIP LOCKED: while one claim is uncommitted, a second worker gets the next job at once.

    Without it the second claim would wait for the first transaction to end.
    """

    async def body(engine: AsyncEngine) -> tuple[Job | None, Job | None]:
        async with engine.begin() as conn:
            await new_job(conn)
            await new_job(conn)
        async with engine.connect() as first, engine.connect() as second:
            a = await claim_job(first, worker_id="w1")
            # `first` has not committed, so its row is still locked.
            async with asyncio.timeout(5):
                b = await claim_job(second, worker_id="w2")
            await first.commit()
            await second.commit()
        return a, b

    a, b = with_engine(database_url, body)
    assert a is not None
    assert b is not None
    assert a.id != b.id


def test_claim_and_step_insert_on_one_job_do_not_block_each_other(database_url: str) -> None:
    """A step insert locks its job row against key changes only; a claim changes no key."""

    async def body(engine: AsyncEngine) -> tuple[Job, Job | None]:
        async with engine.begin() as conn:
            job = await new_job(conn)
            other = await new_job(conn)
        async with engine.connect() as writer, engine.connect() as claimer:
            # An uncommitted step insert does not hide its job from a claim.
            await add_step(writer, job.id, 1)
            async with asyncio.timeout(5):
                claimed = await claim_job(claimer, worker_id="w1")
            await writer.commit()
            # An uncommitted claim does not block a step insert for that job.
            assert await claim_job(claimer, worker_id="w1") is not None
            async with asyncio.timeout(5):
                await add_step(writer, other.id, 1)
            await writer.commit()
            await claimer.commit()
        return job, claimed

    job, claimed = with_engine(database_url, body)
    assert claimed is not None
    assert claimed.id == job.id


def test_claim_statement_uses_the_claim_index(database_url: str) -> None:
    """The planner can serve the statement claim_job sends from the partial index.

    The statement is prepared and planned without its parameter values
    (force_generic_plan), the hardest case: the index only matches if the
    statuses are constants in the SQL. Sequential scans are switched off
    because the planner prefers them on a table this small.
    """
    statement = claim_statement("w1", timedelta(seconds=30)).compile(
        dialect=postgresql.asyncpg.dialect(),  # type: ignore[no-untyped-call]
        compile_kwargs={"render_postcompile": True},
    )
    # One value per $n placeholder, in order, written as SQL literals.
    values = [statement.params[name] for name in statement.positiontup or ()]
    arguments = ", ".join(
        "NULL" if value is None else "'" + str(getattr(value, "value", value)) + "'"
        for value in values
    )

    async def body() -> str:
        # asyncpg directly: with no arguments, execute() sends plain SQL, which
        # PREPARE with $n placeholders needs.
        conn = await asyncpg.connect(database_url, timeout=5)
        try:
            await conn.execute(
                "SET enable_seqscan = off; SET enable_sort = off;"
                " SET plan_cache_mode = force_generic_plan;"
                f" PREPARE claim AS {statement}"
            )
            plan = await conn.fetch(f"EXPLAIN EXECUTE claim({arguments})")
        finally:
            await conn.close()
        return "\n".join(row[0] for row in plan)

    plan = asyncio.run(body())
    assert "Index Scan using jobs_claim_order" in plan
    assert "LockRows" in plan
    # The lease check is part of the scan, so the statement asks for both kinds.
    assert "lease_expires_at < clock_timestamp()" in plan


# Takeover


def test_a_running_job_with_a_live_lease_is_not_claimed(database_url: str) -> None:
    async def body(conn: AsyncConnection) -> tuple[Job | None, Job | None]:
        await new_job(conn)
        first = await claim_job(conn, worker_id="w1", lease=timedelta(seconds=30))
        return first, await claim_job(conn, worker_id="w2")

    first, second = in_transaction(database_url, body)
    assert first is not None
    assert first.takeovers == 0
    assert second is None


def test_a_running_job_with_an_expired_lease_is_taken_over(database_url: str) -> None:
    async def body(conn: AsyncConnection) -> tuple[Job | None, Job | None, datetime]:
        await new_job(conn)
        first = await claim_job(conn, worker_id="w1")
        assert first is not None
        await expire_lease(conn, first.id)
        before = (await conn.execute(text("SELECT clock_timestamp()"))).scalar_one()
        return first, await claim_job(conn, worker_id="w2", lease=timedelta(seconds=30)), before

    first, second, before = in_transaction(database_url, body)
    assert first is not None
    assert second is not None
    assert second.id == first.id
    assert second.status is S.RUNNING
    assert second.lease_owner == "w2"
    assert (second.lease_epoch, second.takeovers) == (2, 1)
    assert second.lease_expires_at is not None
    assert second.lease_expires_at >= before + timedelta(seconds=30)


def test_an_expired_lease_is_claimed_in_its_turn_by_age(database_url: str) -> None:
    """A takeover is not put behind newer queued jobs: the order is by creation time."""

    async def body(engine: AsyncEngine) -> tuple[UUID, UUID, list[UUID]]:
        async with engine.begin() as conn:
            old = (await new_job(conn)).id
            assert await claim_job(conn, worker_id="w1") is not None
            await expire_lease(conn, old)
        async with engine.begin() as conn:
            new = (await new_job(conn)).id
        claimed = []
        for _ in range(2):
            async with engine.begin() as conn:
                job = await claim_job(conn, worker_id="w2")
                assert job is not None
                claimed.append(job.id)
        return old, new, claimed

    old, new, claimed = with_engine(database_url, body)
    assert claimed == [old, new]


def test_20_concurrent_claimers_on_10_expired_jobs_claim_each_job_once(database_url: str) -> None:
    jobs_count, claimers = 10, 20

    async def body(engine: AsyncEngine) -> tuple[set[UUID], list[Job | None]]:
        async with engine.begin() as conn:
            created = {(await new_job(conn)).id for _ in range(jobs_count)}
            # All ten are claimed before any lease expires. A claim made after
            # an expiry would take that job over instead of a new one.
            for _ in range(jobs_count):
                assert await claim_job(conn, worker_id="gone") is not None
            for job_id in created:
                await expire_lease(conn, job_id)

        ready = asyncio.Barrier(claimers)

        async def claimer(n: int) -> Job | None:
            async with engine.connect() as conn, conn.begin():
                await conn.execute(text("SELECT 1"))
                await ready.wait()
                job = await claim_job(conn, worker_id=f"w{n}")
                # Hold the row lock, so the other claims run while it is held.
                await asyncio.sleep(0.2)
                return job

        async with asyncio.timeout(30):
            results = await asyncio.gather(*(claimer(n) for n in range(claimers)))
        return created, list(results)

    created, results = with_engine(database_url, body, connections=claimers)
    claimed = [job for job in results if job is not None]
    assert len(claimed) == jobs_count
    assert {job.id for job in claimed} == created
    assert len({job.lease_owner for job in claimed}) == jobs_count
    assert all((job.lease_epoch, job.takeovers) == (2, 1) for job in claimed)

    async def stored(conn: AsyncConnection) -> list[tuple[str, int, int]]:
        rows = await conn.execute(text("SELECT status, lease_epoch, takeovers FROM jobs"))
        return [(row.status, row.lease_epoch, row.takeovers) for row in rows]

    assert in_transaction(database_url, stored) == [("running", 2, 1)] * jobs_count


def test_a_claim_after_a_status_change_is_not_a_takeover(database_url: str) -> None:
    """Only a claim of a `running` job counts. A job that went back to `queued` does not."""

    async def body(conn: AsyncConnection) -> Job | None:
        await new_job(conn)
        first = await claim_job(conn, worker_id="w1")
        assert first is not None
        await conn.execute(
            text(
                "UPDATE jobs SET status = 'queued', lease_owner = NULL, lease_expires_at = NULL"
                " WHERE id = :id"
            ),
            {"id": first.id},
        )
        return await claim_job(conn, worker_id="w2")

    second = in_transaction(database_url, body)
    assert second is not None
    assert (second.lease_epoch, second.takeovers) == (2, 0)


async def take_over_until_refused(engine: AsyncEngine) -> tuple[UUID, list[int], Job | None]:
    """Claim one job, then let its lease expire and claim again until the claim finds nothing.

    Returns the job id, the takeover count after each claim that handed it
    out, and what the last claim returned.
    """
    async with engine.begin() as conn:
        job_id = (await new_job(conn)).id
        await add_step(conn, job_id, 1)
    counts: list[int] = []
    while True:
        async with engine.begin() as conn:
            job = await claim_job(conn, worker_id=f"w{len(counts)}")
            if job is None or len(counts) > TAKEOVER_LIMIT + 3:
                return job_id, counts, job
            counts.append(job.takeovers)
            await expire_lease(conn, job.id)


def test_the_sixth_takeover_fails_the_job_with_an_error_step(database_url: str) -> None:
    async def body(engine: AsyncEngine) -> tuple[list[int], Job | None, Job | None, list[Any]]:
        job_id, counts, last = await take_over_until_refused(engine)
        async with engine.begin() as conn:
            return counts, last, await get_job(conn, job_id), await read_steps(conn, job_id)

    counts, last, job, steps = with_engine(database_url, body)
    # The first claim and five takeovers were handed out: six runs.
    assert counts == [0, 1, 2, 3, 4, 5]
    assert TAKEOVER_LIMIT == 5
    assert last is None
    assert job is not None
    assert job.status is S.FAILED
    assert job.takeovers == 5
    assert job.lease_epoch == 6
    assert job.lease_owner is None
    assert job.lease_expires_at is None
    assert job.error is not None
    assert "taken over 5 times" in job.error
    # The reason is in the log too, after the step that was already there.
    assert [(step.seq, step.kind) for step in steps] == [
        (1, StepKind.MODEL_REQUEST),
        (2, StepKind.ERROR),
    ]
    assert steps[1].payload == {"message": job.error}
    assert steps[1].lease_epoch == 6


def test_a_claim_that_fails_a_job_at_the_limit_still_hands_out_the_next_job(
    database_url: str,
) -> None:
    async def body(conn: AsyncConnection) -> tuple[Job, Job | None, Job | None]:
        capped = await new_job(conn)
        assert await claim_job(conn, worker_id="w1") is not None
        await conn.execute(
            text("UPDATE jobs SET takeovers = :limit WHERE id = :id"),
            {"limit": TAKEOVER_LIMIT, "id": capped.id},
        )
        await expire_lease(conn, capped.id)
        queued = await new_job(conn)
        claimed = await claim_job(conn, worker_id="w2")
        return queued, claimed, await get_job(conn, capped.id)

    queued, claimed, capped = in_transaction(database_url, body)
    assert claimed is not None
    assert claimed.id == queued.id
    assert capped is not None
    assert capped.status is S.FAILED


def test_a_claim_reports_the_jobs_it_failed_at_the_limit(database_url: str) -> None:
    """The caller has to know: nobody else will ever clean up after such a job."""

    async def body(conn: AsyncConnection) -> tuple[Job, list[Job], Job | None]:
        capped = await new_job(conn)
        assert await claim_job(conn, worker_id="w1") is not None
        await conn.execute(
            text("UPDATE jobs SET takeovers = :limit WHERE id = :id"),
            {"limit": TAKEOVER_LIMIT, "id": capped.id},
        )
        await expire_lease(conn, capped.id)
        failed: list[Job] = []
        claimed = await claim_job(conn, worker_id="w2", failed=failed)
        return capped, failed, claimed

    capped, failed, claimed = in_transaction(database_url, body)
    assert claimed is None
    assert [(job.id, job.status) for job in failed] == [(capped.id, S.FAILED)]


def test_a_failed_claim_of_a_capped_job_rolls_back_with_its_error_step(database_url: str) -> None:
    """The status and the error step are one transaction: both happen or neither."""

    async def body(engine: AsyncEngine) -> tuple[Job | None, list[Any]]:
        async with engine.begin() as conn:
            job = await new_job(conn)
            assert await claim_job(conn, worker_id="w1") is not None
            await conn.execute(
                text("UPDATE jobs SET takeovers = :limit WHERE id = :id"),
                {"limit": TAKEOVER_LIMIT, "id": job.id},
            )
            await expire_lease(conn, job.id)
        async with engine.connect() as conn:
            assert await claim_job(conn, worker_id="w2") is None
            await conn.rollback()
        async with engine.begin() as conn:
            return await get_job(conn, job.id), await read_steps(conn, job.id)

    job, steps = with_engine(database_url, body)
    assert job is not None
    assert job.status is S.RUNNING
    assert steps == []


# Steps


def test_steps_are_read_back_in_seq_order(migrated_database_url: str) -> None:
    async def body(conn: AsyncConnection) -> tuple[UUID, list[Any], list[Any]]:
        job = await new_job(conn)
        for seq in (2, 1, 3):
            await add_step(conn, job.id, seq, {"n": seq})
        # Without index scans the rows come back in insertion order (2, 1, 3)
        # unless the query sorts them. The primary key index would hide a
        # missing ORDER BY.
        await conn.execute(text("SET LOCAL enable_indexscan = off"))
        await conn.execute(text("SET LOCAL enable_bitmapscan = off"))
        return job.id, await read_steps(conn, job.id), await read_steps(conn, job.id, after_seq=2)

    job_id, steps, tail = in_transaction(migrated_database_url, body)
    assert [(s.job_id, s.seq, s.kind, s.payload, s.lease_epoch) for s in steps] == [
        (job_id, n, StepKind.MODEL_REQUEST, {"n": n}, 1) for n in (1, 2, 3)
    ]
    assert [s.seq for s in tail] == [3]


def test_duplicate_seq_is_rejected_and_the_first_step_stays(migrated_database_url: str) -> None:
    async def body(conn: AsyncConnection) -> list[Any]:
        job = await new_job(conn)
        await add_step(conn, job.id, 1, {"writer": "first"})
        with pytest.raises(StepConflict):
            await add_step(conn, job.id, 1, {"writer": "second"})
        # The transaction is still usable after the conflict.
        await add_step(conn, job.id, 2, {"writer": "first"})
        return await read_steps(conn, job.id)

    steps = in_transaction(migrated_database_url, body)
    assert [(s.seq, s.payload) for s in steps] == [
        (1, {"writer": "first"}),
        (2, {"writer": "first"}),
    ]


def test_two_connections_writing_the_same_seq_one_wins(migrated_database_url: str) -> None:
    async def body(engine: AsyncEngine) -> tuple[dict[str, str], list[Any]]:
        async with engine.begin() as conn:
            job = await new_job(conn)
        ready = asyncio.Barrier(2)

        async def writer(name: str) -> str:
            async with engine.connect() as conn, conn.begin():
                await conn.execute(text("SELECT 1"))
                await ready.wait()
                try:
                    await add_step(conn, job.id, 1, {"writer": name})
                except StepConflict:
                    # The loser's transaction is still usable: this write commits.
                    await add_step(conn, job.id, 2, {"writer": name})
                    return "conflict"
                await asyncio.sleep(0.2)
                return "written"

        async with asyncio.timeout(30):
            a, b = await asyncio.gather(writer("a"), writer("b"))
        async with engine.begin() as conn:
            return {"a": a, "b": b}, await read_steps(conn, job.id)

    outcomes, steps = with_engine(migrated_database_url, body)
    assert sorted(outcomes.values()) == ["conflict", "written"]
    winner = next(name for name, outcome in outcomes.items() if outcome == "written")
    loser = next(name for name, outcome in outcomes.items() if outcome == "conflict")
    assert [(s.seq, s.payload) for s in steps] == [(1, {"writer": winner}), (2, {"writer": loser})]


@pytest.mark.parametrize("seq", [0, -1])
def test_seq_below_one_is_refused_before_the_insert(migrated_database_url: str, seq: int) -> None:
    async def body(conn: AsyncConnection) -> list[Any]:
        job = await new_job(conn)
        with pytest.raises(ValueError, match="seq starts at 1"):
            await add_step(conn, job.id, seq)
        await add_step(conn, job.id, 1)
        return await read_steps(conn, job.id)

    assert len(in_transaction(migrated_database_url, body)) == 1


def test_same_seq_in_two_jobs_is_accepted(migrated_database_url: str) -> None:
    async def body(conn: AsyncConnection) -> None:
        for _ in range(2):
            await add_step(conn, (await new_job(conn)).id, 1)

    in_transaction(migrated_database_url, body)


def test_step_for_an_unknown_job_is_rejected(migrated_database_url: str) -> None:
    with pytest.raises(IntegrityError):
        in_transaction(migrated_database_url, lambda c: add_step(c, uuid4(), 1))


def test_steps_of_another_job_are_not_read(migrated_database_url: str) -> None:
    async def body(conn: AsyncConnection) -> list[Any]:
        mine, other = await new_job(conn), await new_job(conn)
        await add_step(conn, other.id, 1)
        return await read_steps(conn, mine.id)

    assert in_transaction(migrated_database_url, body) == []


# Any text a Python str can hold, NUL and unpaired surrogates included, and
# any float, NaN and infinity included.
any_text = st.text(st.characters())
json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats() | any_text,
    lambda children: st.lists(children) | st.dictionaries(any_text, children),
    max_leaves=20,
)


def numbers_as_floats(value: Any) -> Any:
    """jsonb has no int or float type (1e22 is read back as an int), so compare as floats."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, list):
        return [numbers_as_floats(item) for item in value]
    if isinstance(value, dict):
        return {key: numbers_as_floats(item) for key, item in value.items()}
    return value


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(payload=st.dictionaries(any_text, json_values, max_size=5))
def test_a_payload_is_refused_up_front_or_read_back_unchanged(
    migrated_database_url: str, payload: dict[str, Any]
) -> None:
    """No payload reaches the database and fails there.

    Whatever check_json lets through, Postgres stores and returns equal.
    """
    try:
        check_json(payload)
        accepted = True
    except InvalidPayload:
        accepted = False

    async def body(conn: AsyncConnection) -> Any:
        job = await new_job(conn)
        if not accepted:
            with pytest.raises(InvalidPayload):
                await add_step(conn, job.id, 1, payload)
            return None
        await add_step(conn, job.id, 1, payload)
        return (await read_steps(conn, job.id))[0].payload

    stored = in_transaction(migrated_database_url, body)
    if accepted:
        assert numbers_as_floats(stored) == numbers_as_floats(payload)


@pytest.mark.parametrize(
    "bad",
    [
        {"text": "a\x00b"},
        {"a\x00b": 1},
        {"nested": [{"deep": "\x00"}]},
        {"n": float("nan")},
        {"n": [float("inf")]},
        {"text": "\ud800"},
        {"n": (float("nan"),)},
    ],
)
def test_payload_jsonb_cannot_hold_is_rejected_before_the_insert(
    migrated_database_url: str, bad: dict[str, Any]
) -> None:
    async def body(conn: AsyncConnection) -> list[Any]:
        job = await new_job(conn)
        with pytest.raises(InvalidPayload):
            await add_step(conn, job.id, 1, bad)
        with pytest.raises(InvalidPayload):
            await create_job(conn, spec=bad, token_budget=1, deadline=DEADLINE)
        # The transaction is still usable.
        await add_step(conn, job.id, 1, {"ok": True})
        return await read_steps(conn, job.id)

    assert len(in_transaction(migrated_database_url, body)) == 1


# Status


async def job_in(conn: AsyncConnection, status: JobStatus) -> Job:
    """A job put directly into `status`, with a lease if it is running."""
    job = await new_job(conn)
    lease = "lease_owner = 'w1', lease_expires_at = now()," if status is S.RUNNING else ""
    await conn.execute(
        text(f"UPDATE jobs SET {lease} status = :status WHERE id = :id"),  # noqa: S608
        {"status": status, "id": job.id},
    )
    return job


MOVES = [(current, new) for current in JobStatus for new in JobStatus if new is not S.RUNNING]


@pytest.mark.parametrize(("current", "new"), MOVES)
def test_update_status_follows_the_transition_table(
    migrated_database_url: str, current: JobStatus, new: JobStatus
) -> None:
    async def body(conn: AsyncConnection) -> tuple[bool, Job | None]:
        job = await job_in(conn, current)
        try:
            await update_status(conn, job.id, new)
            moved = True
        except InvalidTransition:
            moved = False
        return moved, await get_job(conn, job.id)

    moved, job = in_transaction(migrated_database_url, body)
    assert job is not None
    # A running job belongs to its worker. Nothing moves it from here.
    assert moved == (new in TRANSITIONS[current] and current is not S.RUNNING)
    assert job.status is (new if moved else current)


def test_a_concurrent_status_change_is_not_overwritten(migrated_database_url: str) -> None:
    """Two connections move one queued job. The second waits, then sees the new status.

    `cancelled` is final, so the later move to `failed` is refused, although
    it was valid from the status the second connection first saw.
    """

    async def body(engine: AsyncEngine) -> Job | None:
        async with engine.begin() as conn:
            job = await new_job(conn)
        async with engine.connect() as first, engine.connect() as second:
            await update_status(first, job.id, S.CANCELLED)
            # Blocks on the row lock until `first` commits.
            late = asyncio.create_task(update_status(second, job.id, S.FAILED))
            await asyncio.sleep(0.3)
            assert not late.done()
            await first.commit()
            async with asyncio.timeout(5):
                with pytest.raises(InvalidTransition):
                    await late
            await second.commit()
        async with engine.begin() as conn:
            return await get_job(conn, job.id)

    job = with_engine(migrated_database_url, body)
    assert job is not None
    assert job.status is S.CANCELLED


def test_update_status_refuses_running(migrated_database_url: str) -> None:
    async def body(conn: AsyncConnection) -> None:
        job = await new_job(conn)
        await update_status(conn, job.id, S.RUNNING)

    with pytest.raises(ValueError, match="claim_job"):
        in_transaction(migrated_database_url, body)


def test_update_status_of_an_unknown_job_is_rejected(migrated_database_url: str) -> None:
    with pytest.raises(InvalidTransition):
        in_transaction(migrated_database_url, lambda c: update_status(c, uuid4(), S.CANCELLED))


def test_read_progress_returns_status_and_steps_from_one_statement(
    migrated_database_url: str,
) -> None:
    async def body(conn: AsyncConnection) -> dict[str, Any]:
        job, other = await new_job(conn), await new_job(conn)
        await add_step(conn, other.id, 1)
        empty = await read_progress(conn, job.id)
        for seq in (2, 1, 3):
            await add_step(conn, job.id, seq, {"n": seq})
        # As in the read_steps test: without index scans, only ORDER BY sorts.
        await conn.execute(text("SET LOCAL enable_indexscan = off"))
        await conn.execute(text("SET LOCAL enable_bitmapscan = off"))
        return {
            "missing": await read_progress(conn, uuid4()),
            "empty": empty,
            "all": await read_progress(conn, job.id),
            "after": await read_progress(conn, job.id, after_seq=1, limit=1),
            "past": await read_progress(conn, job.id, after_seq=3),
            "steps": await read_steps(conn, job.id),
        }

    got = in_transaction(migrated_database_url, body)
    assert got["missing"] is None
    assert got["empty"] == (JobStatus.QUEUED, [])
    assert got["all"] == (JobStatus.QUEUED, got["steps"])
    assert got["after"] == (JobStatus.QUEUED, got["steps"][1:2])
    assert got["past"] == (JobStatus.QUEUED, [])


def test_read_progress_sends_one_statement(migrated_database_url: str) -> None:
    """Status and steps must come from one snapshot, so it has to be one statement."""

    async def body(engine: AsyncEngine) -> list[str]:
        async with engine.begin() as conn:
            job = await new_job(conn)
            await add_step(conn, job.id, 1)
        statements: list[str] = []

        def record(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
            statements.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", record)
        async with engine.connect() as conn:
            assert await read_progress(conn, job.id) is not None
        return statements

    statements = with_engine(migrated_database_url, body)
    assert len(statements) == 1
    assert "jobs" in statements[0]
    assert "steps" in statements[0]
