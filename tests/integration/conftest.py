"""Fixtures for tests that need the Postgres server from the compose stack.

DATABASE_URL names the stack's database, as the owner. Tests that change the
schema create their own database on the same server, so they never touch the
stack's. Store and worker code runs as the app role, as in the stack, so a
right it lacks fails a test. APP_DATABASE_URL gives that role's login.
"""

import asyncio
import os
import subprocess
import sys
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg
import httpx2
import pytest
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from runtime.provider.messages import AnthropicProvider
from runtime.store import held
from runtime.store.engine import create_engine, sqlalchemy_url
from runtime.store.jobs import Job, claim_job, create_job, get_job
from runtime.store.steps import Step, StepKind, append_step, read_steps
from runtime.tools.base import CheckAnswer, Tool, ToolCall, ToolRegistry, ToolResult
from runtime.worker.agent import AgentSettings, run_job

REPO_ROOT = Path(__file__).resolve().parents[2]

# The compose stack's development defaults, published on localhost.
DEFAULT_DATABASE_URL = "postgresql://runtime:runtime-dev@127.0.0.1:5432/runtime"
DEFAULT_APP_DATABASE_URL = "postgresql://runtime_app:runtime-app-dev@127.0.0.1:5432/runtime"

DEADLINE = datetime.now(UTC) + timedelta(hours=1)


def with_engine[T](
    database_url: str, body: Callable[[AsyncEngine], Awaitable[T]], *, connections: int = 5
) -> T:
    """Run `body` with an engine that allows `connections` at once, then dispose it."""

    async def run() -> T:
        engine = create_async_engine(
            sqlalchemy_url(database_url), pool_size=connections, max_overflow=0
        )
        try:
            return await body(engine)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def in_transaction[T](database_url: str, body: Callable[[AsyncConnection], Awaitable[T]]) -> T:
    """Run `body` in one transaction and commit it."""

    async def run(engine: AsyncEngine) -> T:
        async with engine.begin() as conn:
            return await body(conn)

    return with_engine(database_url, run)


async def new_job(conn: AsyncConnection) -> Job:
    return await create_job(conn, spec={"task": "hello"}, token_budget=1000, deadline=DEADLINE)


async def add_step(
    conn: AsyncConnection, job_id: UUID, seq: int, payload: dict[str, Any] | None = None
) -> None:
    await append_step(
        conn,
        job_id=job_id,
        seq=seq,
        kind=StepKind.MODEL_REQUEST,
        payload=payload or {},
        lease_epoch=1,
    )


async def ask(
    engine: AsyncEngine,
    job_id: UUID,
    lease_epoch: int,
    reason: held.QuestionReason = held.QuestionReason.BEFORE_RUN,
) -> held.Question:
    """Write a question at the end of the job's log, as a worker does before
    it parks the job. Returns what a park takes.

    Not fenced: a test of a stale park needs the question in the log too.
    """
    async with engine.begin() as conn:
        last = await read_steps(conn, job_id)
        seq = (last[-1].seq if last else 0) + 1
        step = await append_step(
            conn,
            job_id=job_id,
            seq=seq,
            kind=StepKind.APPROVAL_REQUESTED,
            payload={
                "tool_use_id": f"toolu_{seq}",
                "name": "run_shell",
                "input": {"command": "ls"},
                "operation_id": f"{job_id}:toolu_{seq}",
                "reason": "the job's spec asks for a person's approval before this tool runs",
            },
            lease_epoch=lease_epoch,
        )
    return held.Question(step.seq, reason)


async def move_to_the_heap_end(conn: AsyncConnection, job_id: UUID, seq: int) -> None:
    """Rewrite one step unchanged, so its row moves to the end of the table.

    For a test that a read sorts by `seq`: rows inserted in `seq` order come
    back in that order without a sort. Needs the owner, which may switch the
    refusal trigger off for its own session (ADR 0013).
    """
    await conn.execute(text("SET LOCAL session_replication_role = replica"))
    await conn.execute(
        text("UPDATE steps SET payload = payload WHERE job_id = :id AND seq = :seq"),
        {"id": job_id, "seq": seq},
    )
    await conn.execute(text("SET LOCAL session_replication_role = origin"))


async def expire_lease(conn: AsyncConnection, job_id: UUID) -> None:
    """Move the job's lease into the past, as if its worker had stopped renewing it.

    A job that has no lease is left as it is.
    """
    await conn.execute(
        text(
            "UPDATE jobs SET lease_expires_at = clock_timestamp() - interval '1 second'"
            " WHERE id = :id AND lease_expires_at IS NOT NULL"
        ),
        {"id": job_id},
    )


async def database_now(conn: AsyncConnection) -> datetime:
    """The database clock, which decides every deadline, lease and wait.

    A test that measures against one of those reads this, not the host clock.
    Here the two share a kernel; against a remote database they would not.
    """
    now: datetime = (await conn.execute(text("SELECT clock_timestamp()"))).scalar_one()
    return now


type Connection = asyncpg.Connection[asyncpg.Record]


def on[T](database_url: str, body: Callable[[Connection], Awaitable[T]]) -> T:
    """Run `body` with a connection to the database and close it afterwards."""

    async def run() -> T:
        conn = await asyncpg.connect(database_url, timeout=5)
        try:
            return await body(conn)
        finally:
            await conn.close()

    return asyncio.run(run())


def as_owner(database_url: str) -> str:
    """The same database, as the owner that DATABASE_URL names."""
    owner = make_url(os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL))
    database = make_url(database_url).database
    return owner.set(database=database).render_as_string(hide_password=False)


def as_app(database_url: str) -> str:
    """The same database, as the app role that the API and the worker use."""
    app = make_url(os.environ.get("APP_DATABASE_URL", DEFAULT_APP_DATABASE_URL))
    database = make_url(database_url).database
    return app.set(database=database).render_as_string(hide_password=False)


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
def fake_tracker_url() -> str:
    """The compose stack's fake issue tracker, published on localhost."""
    return os.environ.get("FAKE_TRACKER_URL", "http://127.0.0.1:8002")


@pytest.fixture(scope="session")
def fake_receiver_url() -> str:
    """The compose stack's fake notification receiver, published on localhost."""
    return os.environ.get("FAKE_RECEIVER_URL", "http://127.0.0.1:8003")


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
def app_database_url(stack_database_url: str) -> str:
    """The database the compose stack migrated, as the app role."""
    return as_app(stack_database_url)


@pytest.fixture(scope="session")
def migrated_owner_url(stack_database_url: str) -> Iterator[str]:
    """A database with every migration applied, shared by the test run, as the owner.

    Tests that use it insert their own rows and do not change the schema.
    """
    with temporary_database(stack_database_url) as url:
        alembic(url, "upgrade", "head")
        yield url


@pytest.fixture(scope="session")
def migrated_database_url(migrated_owner_url: str) -> str:
    """The shared migrated database, as the app role."""
    return as_app(migrated_owner_url)


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
    """A migrated database for one test, for tests that need the tables to themselves.

    As the app role, as the stack's API and worker connect.
    """
    with temporary_database(stack_database_url, template=_migrated_template) as url:
        yield as_app(url)


# The agent loop: one job against the database and the fake provider

HELLO = {"prompt": "Read hello.txt and say what it holds.", "model": "hello"}


def stub_tool(
    run: Callable[[ToolCall], ToolResult],
    *,
    idempotent: bool = True,
    check: Callable[[ToolCall], CheckAnswer] | None = None,
) -> ToolRegistry:
    """A registry whose `read_file` is the given function.

    Idempotent unless told otherwise, like the real `read_file`.
    """
    schema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    tool = Tool("read_file", "Read a file.", schema, run, idempotent=idempotent, check=check)
    return ToolRegistry([tool])


FIXED = stub_tool(lambda call: ToolResult(f"content of {call.input['path']}"))


@dataclass
class Ran:
    job: Job
    steps: list[Step]
    # The fake provider's log of the requests that carried this job's id.
    requests: list[dict[str, Any]]

    @property
    def kinds(self) -> list[StepKind]:
        return [step.kind for step in self.steps]


@dataclass
class Stack:
    database_url: str
    fake_provider_url: str
    workspace_root: Path

    def settings(
        self, default_model: str = "hello", *, lease: float = 30.0, heartbeat: float = 10.0
    ) -> AgentSettings:
        return AgentSettings(
            default_model=default_model,
            max_tokens=1024,
            workspace_root=self.workspace_root,
            lease=timedelta(seconds=lease),
            heartbeat=heartbeat,
        )

    def run[T](
        self, body: Callable[[AsyncEngine, AnthropicProvider], Awaitable[T]], api_key: str = "test"
    ) -> T:
        async def go() -> T:
            engine = create_engine(self.database_url)
            provider = AnthropicProvider(api_key=api_key, base_url=self.fake_provider_url)
            try:
                return await body(engine, provider)
            finally:
                await provider.aclose()
                await engine.dispose()

        return asyncio.run(go())

    def requests_for(self, job: Job) -> list[dict[str, Any]]:
        log: list[dict[str, Any]] = httpx2.get(
            f"{self.fake_provider_url}/_fake/requests", params={"user_id": str(job.id)}, timeout=10
        ).json()
        return log

    async def create(self, conn: AsyncConnection, spec: dict[str, Any]) -> Job:
        return await create_job(
            conn,
            spec=spec,
            token_budget=100_000,
            deadline=datetime.now(UTC) + timedelta(hours=1),
        )

    def run_one(
        self,
        spec: dict[str, Any],
        tools: ToolRegistry = FIXED,
        default_model: str = "hello",
        api_key: str = "test",
    ) -> Ran:
        """Create one job, claim it, run it, and read back everything about it."""

        async def body(engine: AsyncEngine, provider: AnthropicProvider) -> tuple[Job, list[Step]]:
            async with engine.begin() as conn:
                await self.create(conn, spec)
                claimed = await claim_job(conn, worker_id="test-worker")
            assert claimed is not None
            await run_job(engine, provider, tools, claimed, self.settings(default_model))
            async with engine.begin() as conn:
                job = await get_job(conn, claimed.id)
                assert job is not None
                return job, await read_steps(conn, job.id)

        job, steps = self.run(body, api_key)
        return Ran(job, steps, self.requests_for(job))


@pytest.fixture
def stack(database_url: str, fake_provider_url: str, tmp_path: Path) -> Stack:
    return Stack(database_url, fake_provider_url, tmp_path / "workspaces")


def pytest_sessionstart(session: pytest.Session) -> None:
    """End the session before any test unless every worker is on the fake provider."""
    check = subprocess.run(  # noqa: S603 - fixed program and script
        [sys.executable, str(REPO_ROOT / "scripts" / "check_fake_provider.py")],
        capture_output=True,
        text=True,
        check=False,
    )
    if check.returncode != 0:
        pytest.exit(check.stderr.strip(), returncode=pytest.ExitCode.USAGE_ERROR)
