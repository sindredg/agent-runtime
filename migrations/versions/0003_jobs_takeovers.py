"""Count how often a job was taken over from a worker that went away.

Revision: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Counts claims of a `running` job whose lease expired, and nothing else.
    # `lease_epoch` counts every claim, so it cannot serve as the limit on a
    # job that takes its worker down each time it runs.
    op.add_column(
        "jobs",
        sa.Column("takeovers", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.create_check_constraint("jobs_takeovers_not_negative", "jobs", "takeovers >= 0")


def downgrade() -> None:
    # Dropping the column drops its check constraint with it.
    op.drop_column("jobs", "takeovers")
