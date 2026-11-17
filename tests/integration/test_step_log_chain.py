"""G11: the step log cannot be changed without detection (ADR 0013).

Each barrier has its own tests, so that switching one off fails them:

- the insert trigger hashes every step and moves the head on the job row;
- the refusal triggers turn down UPDATE, DELETE and TRUNCATE, for the owner too;
- the app role lacks the rights (`test_roles.py`), so it cannot get past them;
- the recomputation in `runtime.store.chain` finds what the owner changed
  with the triggers switched off.

The owner switches the triggers off for one transaction with
`session_replication_role = replica`, which a superuser may set.
"""

from datetime import timedelta
from typing import Any
from uuid import UUID

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from runtime.store import held
from runtime.store.chain import Mismatch, UnknownJob, step_hash, verify_job
from runtime.store.jobs import claim_job, get_job
from runtime.store.steps import StepKind, read_steps

from .conftest import (
    DEADLINE,
    Connection,
    add_step,
    alembic,
    as_app,
    as_owner,
    in_transaction,
    new_job,
    on,
    with_engine,
)

# Three steps with payloads that print differently from how they were sent:
# jsonb sorts keys and keeps non-ASCII text as it is.
PAYLOADS: list[dict[str, Any]] = [
    {"b": 1, "a": "æøå"},
    {"x": [1, 2.50], "nested": {"z": None, "y": True}},
    {"text": 'line\nbreak "quoted" \\ back'},
]


async def three_steps(conn: AsyncConnection) -> UUID:
    job = await new_job(conn)
    for seq, payload in enumerate(PAYLOADS, start=1):
        await add_step(conn, job.id, seq, payload)
    return job.id


def make_job(url: str) -> UUID:
    return in_transaction(url, three_steps)


def verify(url: str, job_id: UUID) -> list[Mismatch]:
    async def body(conn: AsyncConnection) -> list[Mismatch]:
        return await verify_job(conn, job_id)

    return in_transaction(url, body)


def head(url: str, job_id: UUID) -> tuple[int, bytes | None]:
    async def body(conn: Connection) -> tuple[int, bytes | None]:
        row = await conn.fetchrow("SELECT head_seq, head_hash FROM jobs WHERE id = $1", job_id)
        assert row is not None
        return row["head_seq"], row["head_hash"]

    return on(url, body)


def stored_hashes(url: str, job_id: UUID) -> list[bytes]:
    async def body(conn: Connection) -> list[bytes]:
        rows = await conn.fetch("SELECT hash FROM steps WHERE job_id = $1 ORDER BY seq", job_id)
        return [row["hash"] for row in rows]

    return on(url, body)


# The insert trigger


def test_the_trigger_and_the_recomputation_give_the_same_chain(database_url: str) -> None:
    job_id = make_job(database_url)
    hashes = stored_hashes(database_url, job_id)

    assert verify(database_url, job_id) == []
    assert len(set(hashes)) == 3
    assert all(len(h) == 32 for h in hashes)
    assert head(database_url, job_id) == (3, hashes[-1])


def test_the_chain_does_not_depend_on_the_session_time_zone(database_url: str) -> None:
    async def body(conn: AsyncConnection) -> tuple[UUID, list[Mismatch]]:
        await conn.execute(text("SET LOCAL TIME ZONE 'Asia/Kathmandu'"))
        job_id = await three_steps(conn)
        await conn.execute(text("SET LOCAL TIME ZONE 'America/St_Johns'"))
        return job_id, await verify_job(conn, job_id)

    job_id, found = in_transaction(database_url, body)
    assert found == []
    assert verify(database_url, job_id) == []


def test_the_first_step_chains_from_the_job_id(database_url: str) -> None:
    job_id = make_job(database_url)

    async def body(conn: Connection) -> asyncpg.Record | None:
        return await conn.fetchrow(
            "SELECT lease_epoch, (extract(epoch FROM created_at) * 1000000)::bigint AS us,"
            " kind, payload::text AS payload, hash FROM steps WHERE job_id = $1 AND seq = 1",
            job_id,
        )

    row = on(database_url, body)
    assert row is not None
    assert row["hash"] == step_hash(
        job_id.bytes,
        job_id=job_id,
        seq=1,
        lease_epoch=row["lease_epoch"],
        created_us=row["us"],
        kind=row["kind"],
        payload=row["payload"],
    )


def test_a_hash_the_writer_sends_is_overwritten(database_url: str) -> None:
    async def body(conn: Connection) -> UUID:
        job_id: UUID = await conn.fetchval(
            "INSERT INTO jobs (spec, token_budget, deadline) VALUES ('{}', 1000, $1) RETURNING id",
            DEADLINE,
        )
        await conn.execute(
            "INSERT INTO steps (job_id, seq, kind, payload, lease_epoch, hash)"
            " VALUES ($1, 1, 'model_request', '{}', 1, '\\x00')",
            job_id,
        )
        return job_id

    job_id = on(database_url, body)
    assert stored_hashes(database_url, job_id) != [b"\x00"]
    assert verify(database_url, job_id) == []


@pytest.mark.parametrize("seq", [3, 5])
@pytest.mark.parametrize("role", ["app", "owner"])
def test_a_step_that_does_not_follow_the_last_one_is_refused(
    database_url: str, seq: int, role: str
) -> None:
    async def body(conn: AsyncConnection) -> UUID:
        job = await new_job(conn)
        await add_step(conn, job.id, 1)
        return job.id

    job_id = in_transaction(database_url, body)
    before = head(database_url, job_id)

    async def gap(conn: AsyncConnection) -> None:
        await add_step(conn, job_id, seq)

    writer = as_owner(database_url) if role == "owner" else database_url
    with pytest.raises(Exception, match="does not follow its last step 1"):
        in_transaction(writer, gap)
    assert head(database_url, job_id) == before
    assert verify(database_url, job_id) == []


def test_the_holders_writes_land_with_the_head(database_url: str) -> None:
    """The fenced writes update the job row in the same statement as the trigger does."""

    async def body(engine: AsyncEngine) -> tuple[int, str, list[Mismatch], int]:
        async with engine.begin() as conn:
            await new_job(conn)
            job = await claim_job(conn, worker_id="w1", lease=timedelta(hours=1))
        assert job is not None
        await held.append_step(
            engine,
            job_id=job.id,
            lease_epoch=job.lease_epoch,
            seq=1,
            kind=StepKind.MODEL_RESPONSE,
            payload={"n": 1},
            tokens=7,
        )
        await held.fail(engine, job_id=job.id, lease_epoch=job.lease_epoch, error="stop")
        async with engine.begin() as conn:
            after = await get_job(conn, job.id)
            assert after is not None
            found = await verify_job(conn, job.id)
            steps = await read_steps(conn, job.id)
        return after.tokens_used, after.status.value, found, len(steps)

    tokens, status, found, count = with_engine(database_url, body)
    assert (tokens, status, found, count) == (7, "failed", [], 2)


# The refusal triggers, against the owner, who has every right


REFUSED = [
    "UPDATE steps SET payload = '{\"changed\": true}' WHERE job_id = $1",
    "DELETE FROM steps WHERE job_id = $1 AND seq = 3",
    "TRUNCATE steps",
]


@pytest.mark.parametrize("statement", REFUSED)
def test_the_owner_is_refused_a_change_while_the_triggers_are_on(
    database_url: str, statement: str
) -> None:
    owner = as_owner(database_url)
    job_id = make_job(database_url)

    async def body(conn: Connection) -> None:
        args = [job_id] if "$1" in statement else []
        await conn.execute(statement, *args)

    with pytest.raises(asyncpg.IntegrityConstraintViolationError, match="append-only"):
        on(owner, body)
    assert verify(database_url, job_id) == []


# The recomputation, against the owner with the triggers off


def tamper(url: str, job_id: UUID, *statements: str) -> None:
    """Run `statements` as the owner with every trigger switched off, and commit."""

    async def body(conn: Connection) -> None:
        async with conn.transaction():
            await conn.execute("SET LOCAL session_replication_role = replica")
            for statement in statements:
                await conn.execute(statement.replace("$job", f"'{job_id}'"))

    on(as_owner(url), body)


TAMPERED: dict[str, tuple[list[str], list[Mismatch]]] = {
    "change": (
        ["UPDATE steps SET payload = '{\"b\": 2}' WHERE job_id = $job AND seq = 2"],
        [Mismatch(2, "hash differs")],
    ),
    "change the epoch": (
        ["UPDATE steps SET lease_epoch = 2 WHERE job_id = $job AND seq = 1"],
        [Mismatch(1, "hash differs")],
    ),
    "change and hash again": (
        [
            "UPDATE steps SET payload = '{\"b\": 2}' WHERE job_id = $job AND seq = 2",
            "UPDATE steps s SET hash = step_hash(p.hash, s.job_id, s.seq, s.lease_epoch,"
            " s.created_at, s.kind, s.payload) FROM steps p"
            " WHERE s.job_id = $job AND s.seq = 2 AND p.job_id = s.job_id AND p.seq = 1",
        ],
        [Mismatch(3, "hash differs")],
    ),
    "delete": (
        ["DELETE FROM steps WHERE job_id = $job AND seq = 2"],
        [Mismatch(3, "follows step 1"), Mismatch(3, "hash differs")],
    ),
    "reorder": (
        [
            "UPDATE steps SET seq = 100 WHERE job_id = $job AND seq = 2",
            "UPDATE steps SET seq = 2 WHERE job_id = $job AND seq = 3",
            "UPDATE steps SET seq = 3 WHERE job_id = $job AND seq = 100",
        ],
        [
            Mismatch(2, "hash differs"),
            Mismatch(3, "hash differs"),
            Mismatch(None, "head hash differs from the last step's"),
        ],
    ),
    "insert out of order": (
        [
            "INSERT INTO steps (job_id, seq, kind, payload, lease_epoch, hash)"
            " VALUES ($job, 5, 'error', '{}', 1, '\\x00')",
        ],
        [
            Mismatch(5, "follows step 3"),
            Mismatch(5, "hash differs"),
            Mismatch(None, "head is step 3, the log ends at 5"),
        ],
    ),
    "write the head": (
        ["UPDATE jobs SET head_seq = 2 WHERE id = $job"],
        [Mismatch(None, "head is step 2, the log ends at 3")],
    ),
    "cut the tail": (
        ["DELETE FROM steps WHERE job_id = $job AND seq = 3"],
        [Mismatch(None, "head is step 3, the log ends at 2")],
    ),
    "cut the tail and hash the last step again": (
        [
            "DELETE FROM steps WHERE job_id = $job AND seq = 3",
            "UPDATE steps SET payload = '{}' WHERE job_id = $job AND seq = 2",
        ],
        [Mismatch(2, "hash differs"), Mismatch(None, "head is step 3, the log ends at 2")],
    ),
}


@pytest.mark.parametrize("case", TAMPERED)
def test_a_change_the_owner_makes_is_found(database_url: str, case: str) -> None:
    statements, expected = TAMPERED[case]
    job_id = make_job(database_url)
    assert verify(database_url, job_id) == []

    tamper(database_url, job_id, *statements)

    assert verify(database_url, job_id) == expected


def test_another_job_is_not_affected(database_url: str) -> None:
    changed, untouched = make_job(database_url), make_job(database_url)
    tamper(database_url, changed, "DELETE FROM steps WHERE job_id = $job AND seq = 1")

    assert verify(database_url, changed) != []
    assert verify(database_url, untouched) == []


def test_a_job_without_steps_verifies_and_an_unknown_one_is_named(database_url: str) -> None:
    async def body(conn: AsyncConnection) -> list[Mismatch]:
        job = await new_job(conn)
        return await verify_job(conn, job.id)

    assert in_transaction(database_url, body) == []
    with pytest.raises(UnknownJob):
        verify(database_url, UUID("00000000-0000-7000-8000-000000000000"))


# The migration


def test_the_migration_hashes_the_steps_that_exist(empty_database_url: str) -> None:
    url = empty_database_url
    alembic(url, "upgrade", "0006")

    async def old_log(conn: Connection) -> list[UUID]:
        ids = []
        for _ in range(2):
            job_id = await conn.fetchval(
                "INSERT INTO jobs (spec, token_budget, deadline)"
                " VALUES ('{}', 1000, $1) RETURNING id",
                DEADLINE,
            )
            for seq in (1, 2, 3):
                await conn.execute(
                    "INSERT INTO steps (job_id, seq, kind, payload, lease_epoch)"
                    " VALUES ($1, $2, 'model_request', $3, 1)",
                    job_id,
                    seq,
                    f'{{"seq": {seq}, "text": "æ"}}',
                )
            ids.append(job_id)
        return ids

    job_ids = on(url, old_log)
    alembic(url, "upgrade", "0007")

    for job_id in job_ids:
        assert verify(url, job_id) == []
        assert head(url, job_id) == (3, stored_hashes(url, job_id)[-1])
    # The log goes on from the head, as the app role.
    app = as_app(url)

    async def next_step(conn: AsyncConnection) -> None:
        await add_step(conn, job_ids[0], 4)

    in_transaction(app, next_step)
    assert verify(url, job_ids[0]) == []


def test_the_downgrade_takes_the_chain_away(empty_database_url: str) -> None:
    url = empty_database_url
    alembic(url, "upgrade", "0007")
    job_id = make_job(as_app(url))
    alembic(url, "downgrade", "0006")

    async def after(conn: Connection) -> tuple[bool, bool, bool]:
        columns = await conn.fetchval(
            "SELECT count(*) FROM information_schema.columns"
            " WHERE table_name IN ('jobs', 'steps') AND column_name LIKE '%hash%'"
        )
        triggers = await conn.fetchval(
            "SELECT count(*) FROM pg_trigger WHERE tgrelid = 'steps'::regclass AND NOT tgisinternal"
        )
        inserts: bool = await conn.fetchval(
            "SELECT has_table_privilege('runtime_app', 'jobs', 'INSERT')"
        )
        return columns == 0, triggers == 0, inserts

    assert on(url, after) == (True, True, True)
    alembic(url, "upgrade", "0007")
    assert verify(url, job_id) == []
