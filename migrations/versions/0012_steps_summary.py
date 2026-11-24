"""Allow the step kind `summary` (ADR 0033).

Revision: 0012
Revises: 0011

A `summary` step holds the model's summary of a long conversation and the
message that opens the next context window. A worker built before it cannot
replay such a log.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | None = None
depends_on: str | None = None

KINDS_BEFORE = (
    "kind IN ('model_request', 'model_response', 'tool_intent', 'tool_result',"
    " 'error', 'approval_requested', 'approval_given', 'approval_denied', 'status_changed',"
    " 'notification_sent')"
)
KINDS_AFTER = (
    "kind IN ('model_request', 'model_response', 'tool_intent', 'tool_result',"
    " 'error', 'approval_requested', 'approval_given', 'approval_denied', 'status_changed',"
    " 'notification_sent', 'summary')"
)


def _replace_kind_check(condition: str) -> None:
    # As in 0005 and 0008: both statements run in one transaction, so the
    # table is never without the check.
    op.drop_constraint("steps_kind_valid", "steps", type_="check")
    op.create_check_constraint("steps_kind_valid", "steps", condition)


def upgrade() -> None:
    _replace_kind_check(KINDS_AFTER)


def downgrade() -> None:
    # The log is append-only and is never rewritten to fit an older schema.
    # Said here by name, before the check would refuse it less clearly.
    summaries = op.get_bind().scalar(sa.text("SELECT count(*) FROM steps WHERE kind = 'summary'"))
    if summaries:
        raise RuntimeError(
            f"cannot downgrade below 0012: the step log holds {summaries} summary steps,"
            " which an older schema cannot hold"
        )
    _replace_kind_check(KINDS_BEFORE)
