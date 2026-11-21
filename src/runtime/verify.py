"""`runtime verify`: recompute the step log's hash chain from the database.

It connects to Postgres, not to the API, so it also checks a stack whose
API is down, or a restored copy of the database (ADR 0016). It only reads,
in a read-only transaction, and the app role is enough.

Exit status: 0 when every chain holds, 1 when a mismatch was found, 2 when a
job could not be checked and no mismatch was found. The database URL is never
printed: it holds a password.
"""

from collections.abc import Sequence
from typing import TextIO
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import ArgumentError, DBAPIError, SQLAlchemyError

from runtime.store.chain import ChainCheck, UnknownJob, verify_job
from runtime.store.engine import create_engine

# The stack's development default for the app role, published on localhost.
DEFAULT_DATABASE_URL = "postgresql://runtime_app:runtime-app-dev@127.0.0.1:5432/runtime"

EXIT_HOLDS = 0
EXIT_MISMATCH = 1
EXIT_CANNOT_CHECK = 2

_ALL_JOBS = text("SELECT id FROM jobs ORDER BY created_at, id")


def _report(job_id: UUID, check: ChainCheck, out: TextIO, *, quiet: bool) -> None:
    if not check.mismatches:
        if not quiet:
            print(f"{job_id}: holds, {check.steps} steps", file=out)
        return
    for mismatch in check.mismatches:
        where = "head" if mismatch.seq is None else f"step {mismatch.seq}"
        print(f"{job_id}: {where}: {mismatch.reason}", file=out)


def _reason(error: BaseException) -> str:
    """One line about a database error, without the URL or the statement."""
    cause: BaseException = error
    if isinstance(error, DBAPIError) and error.orig is not None:
        # The driver's own error is behind SQLAlchemy's adapter.
        cause = error.orig.__cause__ or error.orig
    if isinstance(cause, TypeError) and "unexpected keyword argument" in str(cause):
        # asyncpg names the query key it refused, which can be the tail of a
        # password that was not URL-encoded.
        return "TypeError: the URL has a query parameter the driver does not take"
    lines = str(cause).splitlines()
    return f"{type(cause).__name__}: {lines[0] if lines else ''}"


async def verify(
    database_url: str, job_ids: Sequence[UUID] | None, out: TextIO, err: TextIO
) -> int:
    """Check the chains of `job_ids`, or of every job when it is None. Returns the exit status."""
    mismatched = unknown = checked = 0
    # Checking every job, only the ones with a mismatch are named.
    quiet = job_ids is None
    try:
        engine = create_engine(database_url, application_name="runtime-verify")
    except ArgumentError, ValueError:
        # Never the error's own text: for a port that is not a number the
        # library repeats that part of the URL, which can be the password.
        print("runtime: cannot check: the database URL cannot be read", file=err)
        return EXIT_CANNOT_CHECK
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SET TRANSACTION READ ONLY"))
            # One statement per job: a by-hand check, not a hot path.
            if job_ids is None:
                job_ids = [row.id for row in await conn.execute(_ALL_JOBS)]
            job_ids = list(dict.fromkeys(job_ids))
            for job_id in job_ids:
                try:
                    check = await verify_job(conn, job_id)
                except UnknownJob:
                    print(f"{job_id}: no such job", file=err)
                    unknown += 1
                    continue
                checked += 1
                mismatched += bool(check.mismatches)
                _report(job_id, check, out, quiet=quiet)
            await conn.rollback()
    # TypeError: asyncpg refuses a URL parameter it does not know, such as
    # libpq's `sslmode`. ValueError: it refuses the value of one it knows.
    except (SQLAlchemyError, OSError, TypeError, ValueError) as error:
        print(f"runtime: cannot check the database: {_reason(error)}", file=err)
        return EXIT_MISMATCH if mismatched else EXIT_CANNOT_CHECK
    finally:
        await engine.dispose()
    print(f"{checked} jobs checked, {mismatched} with a mismatch", file=out)
    if mismatched:
        return EXIT_MISMATCH
    return EXIT_CANNOT_CHECK if unknown else EXIT_HOLDS
