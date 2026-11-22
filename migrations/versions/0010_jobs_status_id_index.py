"""Add the index that serves the job list filtered by status.

Revision: 0010
Revises: 0009
"""

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # The list is read newest first, and ids sort by creation time (uuidv7).
    # Without a filter the primary key serves it. With one, this index does:
    # the few jobs that wait for approval are found without reading every
    # finished job on the way.
    op.create_index("jobs_status_id", "jobs", ["status", "id"])


def downgrade() -> None:
    op.drop_index("jobs_status_id", table_name="jobs")
