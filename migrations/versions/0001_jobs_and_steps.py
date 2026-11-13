"""Create the jobs and steps tables.

Revision: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None

# Text with a CHECK instead of a Postgres enum: a later migration can change
# the list by replacing the constraint.
JOB_STATUS_CHECK = (
    "status IN ('queued', 'running', 'waiting', 'needs_approval',"
    " 'succeeded', 'failed', 'cancelled')"
)
STEP_KIND_CHECK = (
    "kind IN ('model_request', 'model_response', 'tool_intent', 'tool_result',"
    " 'error', 'approval_requested', 'approval_given')"
)

NOW = sa.text("now()")


def timestamp() -> sa.DateTime:
    """timestamptz: an absolute instant, independent of the session time zone."""
    return sa.DateTime(timezone=True)


def upgrade() -> None:
    # One row per job.
    op.create_table(
        "jobs",
        # uuidv7() is built into Postgres 18. Its values sort by creation
        # time, so new rows land at the end of the primary key index.
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("status", sa.Text(), server_default="queued", nullable=False),
        sa.Column("spec", postgresql.JSONB(), nullable=False),
        # Every job has a token budget and a deadline; neither is optional.
        sa.Column("token_budget", sa.BigInteger(), nullable=False),
        sa.Column("tokens_used", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("deadline", timestamp(), nullable=False),
        # Earliest time a waiting job may be claimed again.
        sa.Column("not_before", timestamp(), nullable=True),
        # The lease: which worker holds the job and until when. The epoch
        # counts claims, so a write from an earlier holder can be told apart.
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", timestamp(), nullable=True),
        sa.Column("lease_epoch", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", timestamp(), server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="jobs_pkey"),
        sa.CheckConstraint(JOB_STATUS_CHECK, name="jobs_status_valid"),
        sa.CheckConstraint("token_budget >= 0", name="jobs_token_budget_not_negative"),
        sa.CheckConstraint("tokens_used >= 0", name="jobs_tokens_used_not_negative"),
        sa.CheckConstraint("lease_epoch >= 0", name="jobs_lease_epoch_not_negative"),
        # A lease has an owner and an expiry, or neither.
        sa.CheckConstraint(
            "(lease_owner IS NULL) = (lease_expires_at IS NULL)",
            name="jobs_lease_owner_and_expiry_together",
        ),
    )

    # The append-only event log of a job.
    op.create_table(
        "steps",
        sa.Column("job_id", sa.Uuid(), nullable=False),
        # Position in the job's log, starting at 1.
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        # The job's lease epoch when the step was written.
        sa.Column("lease_epoch", sa.Integer(), nullable=False),
        sa.Column("created_at", timestamp(), server_default=NOW, nullable=False),
        # The primary key makes (job_id, seq) unique and is the index for
        # reading one job's steps in order.
        sa.PrimaryKeyConstraint("job_id", "seq", name="steps_pkey"),
        # No ON DELETE action: a job that has steps cannot be deleted.
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], name="steps_job_id_fkey"),
        sa.CheckConstraint("seq >= 1", name="steps_seq_positive"),
        sa.CheckConstraint(STEP_KIND_CHECK, name="steps_kind_valid"),
    )


def downgrade() -> None:
    op.drop_table("steps")
    op.drop_table("jobs")
