"""Add the notifications table and the step kind `notification_sent` (ADR 0029).

Revision: 0011
Revises: 0010

The statement that parks a job for approval also writes a row here: the job,
the question (the `seq` of its `approval_requested` step) and the reason in
one fixed word. The workers deliver the rows to a receiver. A delivery adds a
`notification_sent` step to the job's log.

A job that waits when this migration runs gets its row here, so that it is
notified like a job parked later.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | None = None
depends_on: str | None = None

APP_ROLE = "runtime_app"

KINDS_BEFORE = (
    "kind IN ('model_request', 'model_response', 'tool_intent', 'tool_result',"
    " 'error', 'approval_requested', 'approval_given', 'approval_denied', 'status_changed')"
)
KINDS_AFTER = (
    "kind IN ('model_request', 'model_response', 'tool_intent', 'tool_result',"
    " 'error', 'approval_requested', 'approval_given', 'approval_denied', 'status_changed',"
    " 'notification_sent')"
)


def timestamp() -> sa.DateTime:
    return sa.DateTime(timezone=True)


def _replace_kind_check(condition: str) -> None:
    # As in 0005 and 0008: both statements run in one transaction, so the
    # table is never without the check.
    op.drop_constraint("steps_kind_valid", "steps", type_="check")
    op.create_check_constraint("steps_kind_valid", "steps", condition)


def upgrade() -> None:
    _replace_kind_check(KINDS_AFTER)
    op.create_table(
        "notifications",
        sa.Column("job_id", sa.Uuid(), nullable=False),
        # The `seq` of the question's `approval_requested` step.
        sa.Column("question", sa.Integer(), nullable=False),
        # A fixed word, never the question's reason text: that text can hold
        # a tool check's error, which a target can write.
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        # Due at once. A claim moves it ahead, a failure further.
        sa.Column("next_try", timestamp(), server_default=sa.text("now()"), nullable=False),
        # Set by a 2xx only.
        sa.Column("delivered_at", timestamp(), nullable=True),
        # Set by a 2xx, and when the question was answered or the job is
        # final before one: such a row is not sent.
        sa.Column("closed_at", timestamp(), nullable=True),
        sa.PrimaryKeyConstraint("job_id", "question", name="notifications_pkey"),
        # The question is a step of the job. A key does not check its kind.
        sa.ForeignKeyConstraint(
            ["job_id", "question"],
            ["steps.job_id", "steps.seq"],
            name="notifications_question_fkey",
        ),
        sa.CheckConstraint(
            "reason IN ('before_run', 'interrupted')", name="notifications_reason_valid"
        ),
        sa.CheckConstraint("attempts >= 0", name="notifications_attempts_not_negative"),
        sa.CheckConstraint(
            "delivered_at IS NULL OR closed_at IS NOT NULL",
            name="notifications_delivered_is_closed",
        ),
    )
    # The rows still to send, by when they are due.
    op.create_index(
        "notifications_due",
        "notifications",
        ["next_try"],
        postgresql_where=sa.text("closed_at IS NULL"),
    )
    # The jobs that wait now, on their last question. A question about a call
    # that already had an intent was asked after an interruption: the gate
    # asks before any intent (ADR 0011).
    op.execute(
        """
        INSERT INTO notifications (job_id, question, reason)
        SELECT j.id, q.seq,
               CASE WHEN EXISTS (
                   SELECT FROM steps i
                    WHERE i.job_id = j.id AND i.kind = 'tool_intent' AND i.seq < q.seq
                      AND i.payload->>'tool_use_id' = q.payload->>'tool_use_id'
               ) THEN 'interrupted' ELSE 'before_run' END
          FROM jobs j
          CROSS JOIN LATERAL (
              SELECT s.seq, s.payload FROM steps s
               WHERE s.job_id = j.id AND s.kind = 'approval_requested'
               ORDER BY s.seq DESC LIMIT 1
          ) q
         WHERE j.status = 'needs_approval'
        """
    )
    # By column, as in 0006: a column a later migration adds is not writable
    # by the app until that migration grants it. No DELETE: a row is closed,
    # not removed.
    op.execute(f"GRANT SELECT ON notifications TO {APP_ROLE}")
    op.execute(f"GRANT INSERT (job_id, question, reason) ON notifications TO {APP_ROLE}")
    op.execute(
        f"GRANT UPDATE (attempts, next_try, delivered_at, closed_at) ON notifications TO {APP_ROLE}"
    )


def downgrade() -> None:
    op.drop_index("notifications_due", table_name="notifications")
    op.drop_table("notifications")
    # Fails while the log holds a `notification_sent` step: the log is
    # append-only and is never rewritten to fit an older schema.
    _replace_kind_check(KINDS_BEFORE)
