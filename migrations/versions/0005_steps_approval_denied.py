"""Allow the step kind `approval_denied`.

Revision: 0005
Revises: 0004
"""

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | None = None
depends_on: str | None = None

BEFORE = (
    "kind IN ('model_request', 'model_response', 'tool_intent', 'tool_result',"
    " 'error', 'approval_requested', 'approval_given')"
)
AFTER = (
    "kind IN ('model_request', 'model_response', 'tool_intent', 'tool_result',"
    " 'error', 'approval_requested', 'approval_given', 'approval_denied')"
)


def _replace_kind_check(condition: str) -> None:
    # Both statements run in the migration's one transaction, so the table
    # is never without the check. Adding it reads every step once, under a
    # lock that blocks writers of steps meanwhile.
    op.drop_constraint("steps_kind_valid", "steps", type_="check")
    op.create_check_constraint("steps_kind_valid", "steps", condition)


def upgrade() -> None:
    # A person's refusal of a tool call is a step of its own.
    _replace_kind_check(AFTER)


def downgrade() -> None:
    # Fails while the table holds an `approval_denied` step: the log is
    # never rewritten to fit an older schema.
    _replace_kind_check(BEFORE)
