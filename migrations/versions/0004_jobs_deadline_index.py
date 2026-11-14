"""Add the index that serves the deadline of jobs that no worker holds.

Revision: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Every worker looks for jobs past their deadline once a second. The
    # index holds only jobs that are not final, by deadline, so finished
    # jobs cost that nothing however many there are.
    op.create_index(
        "jobs_open_deadline",
        "jobs",
        ["deadline"],
        postgresql_where=sa.text("status IN ('queued', 'waiting', 'needs_approval', 'running')"),
    )


def downgrade() -> None:
    op.drop_index("jobs_open_deadline", table_name="jobs")
