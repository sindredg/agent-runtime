"""The two database roles, and the check that the services use the right one.

The owner runs the migrations and owns the tables. The app role, used by the
API and the worker, may read and add steps but not change them, and cannot
switch a trigger off (ADR 0013). Migration 0006 grants its rights.
"""

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

# The name migration 0006 grants to.
APP_ROLE = "runtime_app"


class RoleError(Exception):
    """The service's database role could change the step log or switch its protection off."""


@dataclass(frozen=True, slots=True)
class RoleRights:
    name: str
    superuser: bool
    # A member of the role that owns `jobs` or `steps`: it can alter them,
    # and disable or drop their triggers.
    owner: bool
    # Holds UPDATE, DELETE, TRUNCATE or TRIGGER on `steps`.
    changes_steps: bool

    @property
    def problems(self) -> list[str]:
        found = []
        if self.superuser:
            found.append("is a superuser")
        if self.owner:
            found.append("owns the tables")
        if self.changes_steps:
            found.append("may change or delete steps, or add a trigger to them")
        return found


_RIGHTS = text(
    """
    SELECT r.rolname AS name,
           r.rolsuper AS superuser,
           pg_has_role(r.oid, s.relowner, 'MEMBER')
               OR pg_has_role(r.oid, j.relowner, 'MEMBER') AS owner,
           has_any_column_privilege(r.oid, s.oid, 'UPDATE')
               OR has_table_privilege(r.oid, s.oid, 'DELETE, TRUNCATE, TRIGGER')
               AS changes_steps
      FROM pg_roles r,
           pg_class s,
           pg_class j
     WHERE r.rolname = current_user
       AND s.oid = 'public.steps'::regclass
       AND j.oid = 'public.jobs'::regclass
    """
)


async def role_rights(conn: AsyncConnection) -> RoleRights:
    """What the connection's role may do to the tables."""
    row = (await conn.execute(_RIGHTS)).one()
    return RoleRights(
        name=row.name, superuser=row.superuser, owner=row.owner, changes_steps=row.changes_steps
    )


async def check_app_role(conn: AsyncConnection) -> None:
    """Raise RoleError unless the connection's role is limited like the app role.

    The API and the worker call this before they start. Connected as the
    owner, they would run, and the step log's triggers would protect nothing.
    """
    rights = await role_rights(conn)
    if rights.problems:
        raise RoleError(
            f"the database role {rights.name!r} {' and '.join(rights.problems)}."
            f" Connect as {APP_ROLE!r}: DATABASE_URL names the role (ADR 0013)."
        )


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
