"""Fixtures for tests that need the Postgres server from the compose stack.

DATABASE_URL names the stack's database. Tests that change the schema create
their own database on the same server, so they never touch the stack's.
"""

import asyncio
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import asyncpg
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# The compose stack's development default, published on localhost.
DEFAULT_DATABASE_URL = "postgresql://runtime:runtime-dev@127.0.0.1:5432/runtime"


def alembic(database_url: str, *args: str) -> None:
    """Run the alembic command line, as the migrate service does."""
    subprocess.run(  # noqa: S603 - fixed command, arguments come from the tests
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        check=True,
        timeout=60,
    )


async def _admin(database_url: str, statement: str) -> None:
    conn = await asyncpg.connect(database_url, timeout=5)
    try:
        await conn.execute(statement)
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def api_url() -> str:
    """The compose stack's API, published on localhost."""
    return os.environ.get("RUNTIME_API_URL", "http://127.0.0.1:8000")


@pytest.fixture(scope="session")
def fake_provider_url() -> str:
    """The compose stack's fake provider, published on localhost."""
    return os.environ.get("FAKE_PROVIDER_URL", "http://127.0.0.1:8001")


@pytest.fixture(scope="session")
def stack_database_url() -> str:
    """The database the compose stack migrated."""
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)


@contextmanager
def temporary_database(server_url: str, template: str | None = None) -> Iterator[str]:
    """Create a database on the server and drop it on exit.

    It is empty, or a copy of the database named `template`.
    """
    name = f"test_{uuid.uuid4().hex}"
    copy = f' TEMPLATE "{template}"' if template else ""
    asyncio.run(_admin(server_url, f'CREATE DATABASE "{name}"{copy}'))
    try:
        yield f"{server_url.rsplit('/', 1)[0]}/{name}"
    finally:
        asyncio.run(_admin(server_url, f'DROP DATABASE "{name}" WITH (FORCE)'))


@pytest.fixture
def empty_database_url(stack_database_url: str) -> Iterator[str]:
    """A new, empty database for one test."""
    with temporary_database(stack_database_url) as url:
        yield url


@pytest.fixture(scope="session")
def migrated_database_url(stack_database_url: str) -> Iterator[str]:
    """A database with every migration applied, shared by the test run.

    Tests that use it insert their own rows and do not change the schema.
    """
    with temporary_database(stack_database_url) as url:
        alembic(url, "upgrade", "head")
        yield url


@pytest.fixture(scope="session")
def _migrated_template(stack_database_url: str) -> Iterator[str]:
    """A migrated database that nothing connects to, kept to be copied.

    Running the migrations takes over a second. Copying a database takes a
    fraction of that, and Postgres only copies a database with no sessions.
    """
    with temporary_database(stack_database_url) as url:
        alembic(url, "upgrade", "head")
        yield url.rsplit("/", 1)[1]


@pytest.fixture
def database_url(stack_database_url: str, _migrated_template: str) -> Iterator[str]:
    """A migrated database for one test, for tests that need the tables to themselves."""
    with temporary_database(stack_database_url, template=_migrated_template) as url:
        yield url
