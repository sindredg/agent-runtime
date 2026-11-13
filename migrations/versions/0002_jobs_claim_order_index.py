"""Add the index that serves the claim query.

Revision: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # A worker claims the oldest claimable job. The index holds only jobs
    # that are not finished, in claim order, so finished jobs cost the claim
    # nothing however many there are. Running and waiting jobs older than the
    # oldest queued one are still read and passed over.
    op.create_index(
        "jobs_claim_order",
        "jobs",
        ["created_at", "id"],
        postgresql_where=sa.text("status IN ('queued', 'waiting', 'running')"),
    )


def downgrade() -> None:
    op.drop_index("jobs_claim_order", table_name="jobs")
