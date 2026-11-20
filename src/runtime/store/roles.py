"""The two database roles, and the check that the services use the right one.

The owner runs the migrations and owns the tables. The app role, used by the
API and the worker, may read and add steps but not change them, and cannot
switch a trigger off (ADR 0013). Migrations 0006 and 0007 grant its rights.
"""

import asyncio
import contextlib
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from runtime.store.engine import DatabaseUrlError

# The name migration 0006 grants to.
APP_ROLE = "runtime_app"

# Seconds between two tries of the start check while the database cannot be reached.
RETRY_SECONDS = 2.0

log = logging.getLogger(__name__)


class RoleError(Exception):
    """The service's database role could change the step log or switch its protection off."""


# One row: what the connection's role may do that the app role may not.
_RIGHTS = text(
    """
    SELECT r.rolname AS name,
           r.rolsuper AS superuser,
           -- A member of the owner can alter the tables, and disable or drop
           -- their triggers.
           pg_has_role(r.oid, s.relowner, 'MEMBER')
               OR pg_has_role(r.oid, j.relowner, 'MEMBER') AS owner,
           has_any_column_privilege(r.oid, s.oid, 'UPDATE')
               OR has_table_privilege(r.oid, s.oid, 'DELETE, TRUNCATE, TRIGGER')
               AS changes_steps,
           has_column_privilege(r.oid, j.oid, 'head_seq', 'INSERT, UPDATE')
               OR has_column_privilege(r.oid, j.oid, 'head_hash', 'INSERT, UPDATE')
               AS writes_head,
           has_function_privilege(r.oid, 'public.steps_chain()', 'EXECUTE') AS runs_chain,
           has_schema_privilege(r.oid, 'public', 'CREATE') AS creates
      FROM pg_roles r,
           pg_class s,
           pg_class j
     WHERE r.rolname = current_user
       AND s.oid = 'public.steps'::regclass
       AND j.oid = 'public.jobs'::regclass
    """
)


async def check_app_role(conn: AsyncConnection) -> None:
    """Raise RoleError unless the connection's role is limited like the app role.

    Connected as the owner, the API and the worker would run, and nothing in
    the database would stop them changing a step.
    """
    row = (await conn.execute(_RIGHTS)).one()
    problems = [
        problem
        for found, problem in [
            (row.superuser, "is a superuser"),
            (row.owner, "owns the tables"),
            (row.changes_steps, "may change or delete steps, or add a trigger to them"),
            (row.writes_head, "may write the head of the step log"),
            (row.runs_chain, "may hang the chain's trigger on a table of its own"),
            (row.creates, "may create objects in the schema"),
        ]
        if found
    ]
    if problems:
        raise RoleError(
            f"the database role {row.name!r} {' and '.join(problems)}."
            f" Connect as {APP_ROLE!r}: DATABASE_URL names the role (ADR 0013)."
        )


async def wait_for_app_role(
    engine: AsyncEngine, stop: asyncio.Event, retry_seconds: float = RETRY_SECONDS
) -> bool:
    """Run the start check once the database answers. False if `stop` was set first.

    The API and the worker call this before they start. A database that
    cannot be reached yet is tried again, as the worker's loop does. A role
    with too many rights raises RoleError at once: waiting does not fix it.
    """
    while not stop.is_set():
        try:
            async with engine.connect() as conn:
                await check_app_role(conn)
        except RoleError:
            raise
        except TypeError:
            # asyncpg refuses a query key of the URL it does not know. Trying
            # again does not help, and the key may be the tail of a password.
            raise DatabaseUrlError(
                "the database URL has a query parameter the driver does not take;"
                " its password must be URL-safe"
            ) from None
        except Exception:
            log.warning("the database role could not be checked; trying again", exc_info=True)
        else:
            return True
        # The pause before the next try ends early when the service is stopped.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), retry_seconds)
    return False


async def allow_login(conn: AsyncConnection, password: str, role: str = APP_ROLE) -> None:
    """Let `role` log in with `password`. Run as the owner, after the migrations.

    `ALTER ROLE` takes no bind parameters. The role and the password go in as
    settings local to the transaction, and `format` quotes both in the
    server, so nothing is quoted here.
    """
    if not password:
        raise ValueError("the app role's password is empty")
    await conn.execute(
        text(
            "SELECT set_config('runtime.login_role', :role, true),"
            " set_config('runtime.login_password', :password, true)"
        ),
        {"role": role, "password": password},
    )
    await conn.execute(
        text(
            """
            DO $$
            BEGIN
                EXECUTE format(
                    'ALTER ROLE %I LOGIN PASSWORD %L',
                    current_setting('runtime.login_role'),
                    current_setting('runtime.login_password')
                );
            END
            $$
            """
        )
    )
