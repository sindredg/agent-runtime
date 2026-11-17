"""The API and the worker use a role that cannot change the step log or switch its protection off.

ADR 0013. The owner runs the migrations; the app role may read and add steps,
and read, add and update jobs, and nothing else. The services refuse to start
as a role that can do more. The rights are read from Postgres itself, and
each refusal is an attempt that Postgres turns down.
"""

import asyncio
import os
import subprocess
import sys
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

import asyncpg
import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import make_url, text

from runtime.api.app import create_app
from runtime.config import ApiConfig
from runtime.store.engine import create_engine
from runtime.store.roles import (
    APP_ROLE,
    RoleError,
    allow_login,
    check_app_role,
    wait_for_app_role,
)

from .conftest import (
    DEFAULT_APP_DATABASE_URL,
    REPO_ROOT,
    Connection,
    alembic,
    as_app,
    as_owner,
    on,
)

ADD_JOB = (
    "INSERT INTO jobs (spec, token_budget, deadline)"
    " VALUES ('{}', 1000, now() + interval '1 hour') RETURNING id"
)
ADD_STEP = (
    "INSERT INTO steps (job_id, seq, kind, payload, lease_epoch)"
    " VALUES ($1, 1, 'model_request', '{}', 1)"
)


def app_password() -> str:
    password = make_url(os.environ.get("APP_DATABASE_URL", DEFAULT_APP_DATABASE_URL)).password
    assert password
    return password


# What the app role may not do. Each is refused by Postgres for lack of a right.
REFUSED = [
    "UPDATE steps SET payload = '{\"changed\": true}'",
    "DELETE FROM steps",
    "TRUNCATE steps",
    "DELETE FROM jobs",
    "ALTER TABLE steps DISABLE TRIGGER ALL",
    "DROP TABLE steps CASCADE",
    "CREATE TRIGGER extra BEFORE INSERT ON steps FOR EACH ROW"
    " EXECUTE FUNCTION suppress_redundant_updates_trigger()",
    "CREATE TABLE extra (x int)",
    "SET session_replication_role = replica",
]


@pytest.mark.parametrize("statement", REFUSED)
def test_the_app_role_is_refused(database_url: str, statement: str) -> None:
    async def body(conn: Connection) -> None:
        # The role adds the rows it then tries to change.
        job_id = await conn.fetchval(ADD_JOB)
        await conn.execute(ADD_STEP, job_id)
        await conn.execute(statement)

    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        on(database_url, body)


def test_the_app_role_may_update_every_column_of_a_job(database_url: str) -> None:
    """A column that a migration adds without a grant shows up here."""

    async def body(conn: Connection) -> dict[str, bool]:
        rows = await conn.fetch(
            "SELECT column_name, has_column_privilege(current_user, 'jobs', column_name, 'UPDATE')"
            " AS allowed FROM information_schema.columns"
            " WHERE table_schema = 'public' AND table_name = 'jobs'"
        )
        return {row["column_name"]: row["allowed"] for row in rows}

    allowed = on(database_url, body)
    assert allowed
    assert [column for column, ok in allowed.items() if not ok] == []


def test_the_rights_leave_with_their_migration(empty_database_url: str) -> None:
    def rights() -> bool:
        async def body(conn: Connection) -> bool:
            granted: bool = await conn.fetchval(
                "SELECT has_table_privilege($1, 'steps', 'SELECT, INSERT')"
                " OR has_any_column_privilege($1, 'jobs', 'SELECT, INSERT, UPDATE')",
                APP_ROLE,
            )
            return granted

        return on(empty_database_url, body)

    alembic(empty_database_url, "upgrade", "0005")
    assert not rights()
    alembic(empty_database_url, "upgrade", "0006")
    assert rights()
    alembic(empty_database_url, "downgrade", "0005")
    assert not rights()


# The start check


def check(database_url: str, *setup: str) -> None:
    """Run the start check on `database_url` after `setup`, then roll everything back."""

    async def run() -> None:
        engine = create_engine(database_url)
        try:
            async with engine.connect() as conn:
                try:
                    for statement in setup:
                        await conn.execute(text(statement))
                    await check_app_role(conn)
                finally:
                    await conn.rollback()
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_the_check_passes_the_app_role(database_url: str) -> None:
    check(database_url)


def test_the_check_refuses_the_owner(database_url: str) -> None:
    with pytest.raises(RoleError, match="is a superuser and owns the tables"):
        check(as_owner(database_url))


# A role made for the test, in a transaction that is rolled back.
ROLE = "runtime_check_test"


@pytest.mark.parametrize(
    ("grant", "problem"),
    [
        ("GRANT {owner} TO {role}", "owns the tables"),
        ("GRANT UPDATE (payload) ON steps TO {role}", "may change or delete steps"),
        ("GRANT DELETE ON steps TO {role}", "may change or delete steps"),
        ("GRANT TRUNCATE ON steps TO {role}", "may change or delete steps"),
        ("GRANT TRIGGER ON steps TO {role}", "add a trigger to them"),
        ("GRANT CREATE ON SCHEMA public TO {role}", "may create objects in the schema"),
    ],
)
def test_the_check_refuses_a_role_with_one_right_too_many(
    database_url: str, stack_database_url: str, grant: str, problem: str
) -> None:
    owner = make_url(stack_database_url).username
    with pytest.raises(RoleError, match=problem):
        check(
            as_owner(database_url),
            f"CREATE ROLE {ROLE} NOLOGIN",
            grant.format(owner=owner, role=ROLE),
            f"SET LOCAL ROLE {ROLE}",
        )


def test_the_api_does_not_start_as_the_owner(database_url: str) -> None:
    def config(url: str) -> ApiConfig:
        return ApiConfig(
            database_url=url,
            default_token_budget=1000,
            default_timeout_seconds=60,
            host="127.0.0.1",
            port=0,
        )

    with TestClient(create_app(config(database_url)), base_url="http://localhost") as client:
        assert client.get("/healthz").status_code == 200

    owner = create_app(config(as_owner(database_url)))
    with pytest.raises(RoleError, match="superuser"), TestClient(owner):
        pass


def test_the_worker_does_not_start_as_the_owner(
    database_url: str, fake_provider_url: str, tmp_path: Path
) -> None:
    env = {
        "PATH": os.environ["PATH"],
        "DATABASE_URL": as_owner(database_url),
        "ANTHROPIC_BASE_URL": fake_provider_url,
        "ANTHROPIC_API_KEY": "not-a-real-key",
        "MODEL": "hello",
        "WORKSPACE_ROOT": str(tmp_path / "workspaces"),
        "WORKER_ALIVE_FILE": str(tmp_path / "alive"),
    }
    # Without the check the worker runs until it is stopped: the timeout ends
    # the test then, and it fails.
    done = subprocess.run(
        [sys.executable, "-m", "runtime.worker"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode != 0
    assert "RoleError" in done.stderr
    assert "superuser" in done.stderr


def test_the_start_check_waits_until_the_database_lets_it_in(
    database_url: str, stack_database_url: str
) -> None:
    """A role that may not log in yet stands for a database that does not answer yet."""
    role = f"runtime_wait_test_{uuid.uuid4().hex[:8]}"
    password = "wait-test"  # noqa: S105
    url = make_url(database_url).set(username=role, password=password)

    async def run() -> bool:
        engine = create_engine(url.render_as_string(hide_password=False))
        owner = create_engine(stack_database_url)
        try:
            waiting = asyncio.create_task(
                wait_for_app_role(engine, asyncio.Event(), retry_seconds=0.1)
            )
            await asyncio.sleep(1)
            # Refused several times by now, and still trying.
            assert not waiting.done()
            async with owner.begin() as conn:
                await allow_login(conn, password, role=role)
            return await asyncio.wait_for(waiting, 5)
        finally:
            await owner.dispose()
            await engine.dispose()

    async def create(conn: Connection) -> None:
        await conn.execute(f"CREATE ROLE {role} NOLOGIN")

    async def drop(conn: Connection) -> None:
        await conn.execute(f"DROP ROLE IF EXISTS {role}")

    on(stack_database_url, create)
    try:
        assert asyncio.run(run()) is True
    finally:
        on(stack_database_url, drop)


# The stack


def test_the_stacks_api_and_worker_connect_as_the_app_role(
    api_url: str, stack_database_url: str
) -> None:
    # The API connects on its first request that reads the database.
    response = httpx2.get(f"{api_url}/jobs/{uuid.uuid4()}", timeout=10)
    assert response.status_code == 404

    async def body(conn: Connection) -> set[tuple[str, str]]:
        rows = await conn.fetch(
            "SELECT DISTINCT application_name, usename FROM pg_stat_activity"
            " WHERE application_name IN ('runtime-api', 'runtime-worker')"
            # Tests start their own API and worker against other databases.
            " AND datname = current_database()"
        )
        return {(row["application_name"], row["usename"]) for row in rows}

    seen = on(stack_database_url, body)
    assert seen == {("runtime-api", APP_ROLE), ("runtime-worker", APP_ROLE)}


# Setting the login


def migrate(database_url: str, password: str | None) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if key != "APP_DATABASE_PASSWORD"}
    env["DATABASE_URL"] = database_url
    if password is not None:
        env["APP_DATABASE_PASSWORD"] = password
    return subprocess.run(
        [sys.executable, "-m", "runtime.store.migrate"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_migrate_applies_the_schema_and_the_app_role_logs_in(empty_database_url: str) -> None:
    # The stack's own password, so the stack's services keep logging in.
    done = migrate(empty_database_url, app_password())
    assert done.returncode == 0, done.stderr
    check(as_app(empty_database_url))


def test_migrate_needs_the_app_password(empty_database_url: str) -> None:
    done = migrate(empty_database_url, None)
    assert done.returncode != 0
    assert "APP_DATABASE_PASSWORD is not set" in done.stderr

    async def tables(conn: Connection) -> int:
        count: int = await conn.fetchval(
            "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'"
        )
        return count

    assert on(empty_database_url, tables) == 0


def test_a_password_with_quotes_is_set_as_given(stack_database_url: str) -> None:
    """`allow_login` quotes in the server. A role of the test's own, dropped afterwards."""
    role = f"Login Test {uuid.uuid4().hex[:8]}"
    password = "it's a \"pw\" \\ with $$ and '; DROP ROLE runtime_app; --"  # noqa: S105

    async def as_owner_run(body: Callable[[Connection], Awaitable[None]]) -> None:
        conn = await asyncpg.connect(stack_database_url, timeout=5)
        try:
            await body(conn)
        finally:
            await conn.close()

    async def create(conn: Connection) -> None:
        await conn.execute(f'CREATE ROLE "{role}" NOLOGIN')

    async def drop(conn: Connection) -> None:
        await conn.execute(f'DROP ROLE IF EXISTS "{role}"')

    async def set_login() -> None:
        engine = create_engine(stack_database_url)
        try:
            async with engine.begin() as conn:
                await allow_login(conn, password, role=role)
        finally:
            await engine.dispose()

    async def log_in() -> str:
        url = make_url(stack_database_url)
        conn = await asyncpg.connect(
            host=url.host, port=url.port, database=url.database, user=role, password=password
        )
        try:
            name: str = await conn.fetchval("SELECT current_user")
            return name
        finally:
            await conn.close()

    asyncio.run(as_owner_run(create))
    try:
        asyncio.run(set_login())
        assert asyncio.run(log_in()) == role
    finally:
        asyncio.run(as_owner_run(drop))

    async def app_role_exists(conn: Connection) -> bool:
        found: bool = await conn.fetchval(
            "SELECT EXISTS (SELECT FROM pg_roles WHERE rolname = $1)", APP_ROLE
        )
        return found

    assert on(stack_database_url, app_role_exists)
