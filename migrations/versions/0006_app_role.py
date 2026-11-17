"""Give the app role, which the API and the worker use, its rights.

Revision: 0006
Revises: 0005

The owner (the role that runs the migrations) keeps everything else. The app
role may read and add steps, and read, add and update jobs. It may not change
or delete a step, alter a table, or switch a trigger off (ADR 0013).

The role is created without a login if it is missing. `runtime.store.migrate`
sets its login and password after the migrations, so no password is ever in
a migration. A role belongs to the whole server, not to one database, so the
downgrade takes its rights away and leaves the role.
"""

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | None = None
depends_on: str | None = None

APP_ROLE = "runtime_app"

# Granted by column, not on the table, so a column that a later migration
# adds is not writable by the app until that migration grants it. The head of
# the step log is meant to be such a column.
JOB_COLUMNS = (
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
    "takeovers",
    "cancel_requested",
    "error",
    "created_at",
)


def upgrade() -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                CREATE ROLE {APP_ROLE} NOLOGIN;
            END IF;
        END
        $$
        """
    )
    op.execute(f"GRANT SELECT, INSERT ON steps TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON jobs TO {APP_ROLE}")
    op.execute(f"GRANT UPDATE ({', '.join(JOB_COLUMNS)}) ON jobs TO {APP_ROLE}")


def downgrade() -> None:
    # Revoking on a table also revokes the rights on its columns.
    op.execute(f"REVOKE ALL ON steps, jobs FROM {APP_ROLE}")
