"""The migrations apply to an empty database and downgrade cleanly.

Each test runs against a real Postgres server and its own new database.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from .conftest import REPO_ROOT, alembic

type Connection = asyncpg.Connection[asyncpg.Record]

JOB_COLUMNS = {
    "id",
    "status",
    "spec",
    "token_budget",
    "tokens_used",
    "deadline",
    "not_before",
    "lease_owner",
    "lease_expires_at",
    "lease_epoch",
    "cancel_requested",
    "error",
    "created_at",
}
STEP_COLUMNS = {"job_id", "seq", "kind", "payload", "lease_epoch", "created_at"}

JOB_STATUSES = [
    "queued",
    "running",
    "waiting",
    "needs_approval",
    "succeeded",
    "failed",
    "cancelled",
]
STEP_KINDS = [
    "model_request",
    "model_response",
    "tool_intent",
    "tool_result",
    "error",
    "approval_requested",
    "approval_given",
]

DEADLINE = datetime.now(UTC) + timedelta(hours=1)

INSERT_JOB = "INSERT INTO jobs (spec, token_budget, deadline) VALUES ('{}', 1000, $1) RETURNING id"
INSERT_STEP = (
    "INSERT INTO steps (job_id, seq, kind, payload, lease_epoch) VALUES ($1, $2, $3, '{}', 1)"
)


def on[T](database_url: str, body: Callable[[Connection], Awaitable[T]]) -> T:
    """Run `body` with a connection to the database and close it afterwards."""

    async def run() -> T:
        conn = await asyncpg.connect(database_url, timeout=5)
        try:
            return await body(conn)
        finally:
            await conn.close()

    return asyncio.run(run())


async def public_objects(conn: Connection) -> set[tuple[str, str]]:
    """Every table, index, sequence, view, type and function in the public schema.

    Alembic's own version table is left out; it stays after a downgrade.
    """
    rows = await conn.fetch(
        """
        SELECT 'relation' AS catalog, relname AS name FROM pg_class
         WHERE relnamespace = 'public'::regnamespace
        UNION ALL
        SELECT 'type', typname FROM pg_type WHERE typnamespace = 'public'::regnamespace
        UNION ALL
        SELECT 'function', proname FROM pg_proc WHERE pronamespace = 'public'::regnamespace
        """
    )
    return {(r["catalog"], r["name"]) for r in rows if "alembic_version" not in r["name"]}


async def columns(conn: Connection, table: str) -> set[str]:
    rows = await conn.fetch(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_schema = 'public' AND table_name = $1",
        table,
    )
    return {r["column_name"] for r in rows}


async def current_revision(conn: Connection) -> str | None:
    revision: str | None = await conn.fetchval("SELECT version_num FROM alembic_version")
    return revision


def head_revision() -> str | None:
    """The newest revision in the migrations directory."""
    config = Config(toml_file=REPO_ROOT / "pyproject.toml")
    return ScriptDirectory.from_config(config).get_current_head()


def test_upgrade_then_downgrade_then_upgrade(empty_database_url: str) -> None:
    url = empty_database_url
    assert on(url, public_objects) == set()

    alembic(url, "upgrade", "head")
    assert on(url, lambda c: columns(c, "jobs")) == JOB_COLUMNS
    assert on(url, lambda c: columns(c, "steps")) == STEP_COLUMNS
    assert on(url, current_revision) == head_revision()

    # Downgrading removes everything the migrations created.
    alembic(url, "downgrade", "base")
    assert on(url, public_objects) == set()
    assert on(url, current_revision) is None

    # What the downgrade leaves behind accepts the migrations again.
    alembic(url, "upgrade", "head")
    assert on(url, current_revision) == head_revision()


def test_stack_database_is_at_head(stack_database_url: str) -> None:
    """The migrate service brought the stack's own database to the newest revision."""
    assert on(stack_database_url, current_revision) == head_revision()


def test_new_job_gets_defaults(migrated_database_url: str) -> None:
    async def body(conn: Connection) -> asyncpg.Record | None:
        job_id = await conn.fetchval(INSERT_JOB, DEADLINE)
        return await conn.fetchrow("SELECT * FROM jobs WHERE id = $1", job_id)

    job = on(migrated_database_url, body)
    assert job is not None
    assert job["id"].version == 7
    assert job["status"] == "queued"
    assert job["tokens_used"] == 0
    assert job["lease_epoch"] == 0
    assert job["cancel_requested"] is False
    assert job["lease_owner"] is None
    assert job["lease_expires_at"] is None
    assert job["not_before"] is None
    assert job["error"] is None
    assert job["created_at"] is not None


def test_every_job_status_and_step_kind_is_accepted(migrated_database_url: str) -> None:
    async def body(conn: Connection) -> None:
        job_id = await conn.fetchval(INSERT_JOB, DEADLINE)
        for status in JOB_STATUSES:
            await conn.execute("UPDATE jobs SET status = $1 WHERE id = $2", status, job_id)
        for seq, kind in enumerate(STEP_KINDS, start=1):
            await conn.execute(INSERT_STEP, job_id, seq, kind)

    on(migrated_database_url, body)


@pytest.mark.parametrize(
    "change",
    [
        "status = 'paused'",
        "tokens_used = -1",
        "token_budget = -1",
        "lease_epoch = -1",
        "lease_owner = 'worker-1'",
        "lease_expires_at = now()",
        "token_budget = NULL",
        "deadline = NULL",
        "spec = NULL",
    ],
)
def test_job_rejects(migrated_database_url: str, change: str) -> None:
    async def body(conn: Connection) -> None:
        job_id = await conn.fetchval(INSERT_JOB, DEADLINE)
        await conn.execute(f"UPDATE jobs SET {change} WHERE id = $1", job_id)  # noqa: S608

    with pytest.raises(asyncpg.IntegrityConstraintViolationError):
        on(migrated_database_url, body)


def test_lease_owner_and_expiry_are_accepted_together(migrated_database_url: str) -> None:
    async def body(conn: Connection) -> None:
        job_id = await conn.fetchval(INSERT_JOB, DEADLINE)
        await conn.execute(
            "UPDATE jobs SET lease_owner = 'worker-1', lease_expires_at = now() WHERE id = $1",
            job_id,
        )

    on(migrated_database_url, body)


def test_duplicate_step_seq_is_rejected(migrated_database_url: str) -> None:
    async def body(conn: Connection) -> None:
        job_id = await conn.fetchval(INSERT_JOB, DEADLINE)
        await conn.execute(INSERT_STEP, job_id, 1, "model_request")
        await conn.execute(INSERT_STEP, job_id, 1, "model_response")

    with pytest.raises(asyncpg.UniqueViolationError):
        on(migrated_database_url, body)


def test_same_seq_in_two_jobs_is_accepted(migrated_database_url: str) -> None:
    async def body(conn: Connection) -> None:
        for _ in range(2):
            job_id = await conn.fetchval(INSERT_JOB, DEADLINE)
            await conn.execute(INSERT_STEP, job_id, 1, "model_request")

    on(migrated_database_url, body)


def test_step_for_unknown_job_is_rejected(migrated_database_url: str) -> None:
    async def body(conn: Connection) -> None:
        await conn.execute(INSERT_STEP, "00000000-0000-7000-8000-000000000000", 1, "model_request")

    with pytest.raises(asyncpg.ForeignKeyViolationError):
        on(migrated_database_url, body)


def test_job_with_steps_cannot_be_deleted(migrated_database_url: str) -> None:
    async def body(conn: Connection) -> None:
        job_id = await conn.fetchval(INSERT_JOB, DEADLINE)
        await conn.execute(INSERT_STEP, job_id, 1, "model_request")
        await conn.execute("DELETE FROM jobs WHERE id = $1", job_id)

    with pytest.raises(asyncpg.ForeignKeyViolationError):
        on(migrated_database_url, body)


@pytest.mark.parametrize(("seq", "kind"), [(0, "model_request"), (1, "thought")])
def test_step_rejects(migrated_database_url: str, seq: int, kind: str) -> None:
    async def body(conn: Connection) -> None:
        job_id = await conn.fetchval(INSERT_JOB, DEADLINE)
        await conn.execute(INSERT_STEP, job_id, seq, kind)

    with pytest.raises(asyncpg.CheckViolationError):
        on(migrated_database_url, body)
