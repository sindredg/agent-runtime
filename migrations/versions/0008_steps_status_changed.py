"""Allow the step kind `status_changed`.

Revision: 0008
Revises: 0007

A cancel and a park for approval change a job's status. Each now writes a
step that says from what, to what, and why (APTS AR-002).
"""

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | None = None
depends_on: str | None = None

BEFORE = (
    "kind IN ('model_request', 'model_response', 'tool_intent', 'tool_result',"
    " 'error', 'approval_requested', 'approval_given', 'approval_denied')"
)
AFTER = (
    "kind IN ('model_request', 'model_response', 'tool_intent', 'tool_result',"
    " 'error', 'approval_requested', 'approval_given', 'approval_denied', 'status_changed')"
)


def _replace_kind_check(condition: str) -> None:
    # As in 0005: both statements run in one transaction, so the table is
    # never without the check.
    op.drop_constraint("steps_kind_valid", "steps", type_="check")
    op.create_check_constraint("steps_kind_valid", "steps", condition)


def upgrade() -> None:
    _replace_kind_check(AFTER)


def downgrade() -> None:
    # Fails while the table holds a `status_changed` step: the log is append-
    # only and is never rewritten to fit an older schema.
    _replace_kind_check(BEFORE)
