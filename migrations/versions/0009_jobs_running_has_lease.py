"""Refuse a running job without a lease (ADR 0023).

Revision: 0009
Revises: 0008

Nothing ends a `running` job that has no lease: the claim and the deadline
sweep take a job whose lease is over, and NULL never is. A row like that
first gets a lease that is already over, so the next claim ends it or takes
it over on its tested path. Then the constraint, which checks every row.
"""

import logging

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | None = None
depends_on: str | None = None

log = logging.getLogger(__name__)

# The owner of a repaired lease. No worker has this name.
OWNER = "migration 0009"
CONDITION = "status <> 'running' OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)"


def upgrade() -> None:
    # `now()` is the start of this transaction, so the lease is over before
    # any claim can read the row.
    repaired = op.get_bind().execute(
        sa.text(
            "UPDATE jobs SET lease_owner = :owner, lease_expires_at = now()"
            " WHERE status = 'running' AND (lease_owner IS NULL OR lease_expires_at IS NULL)"
        ),
        {"owner": OWNER},
    )
    log.info("running jobs without a lease given one that is over: %d", repaired.rowcount)
    # Plain, not NOT VALID: in the same transaction, after the repair.
    op.create_check_constraint("jobs_running_has_lease", "jobs", CONDITION)


def downgrade() -> None:
    # A repaired lease stays: it is valid under the older schema too.
    op.drop_constraint("jobs_running_has_lease", "jobs", type_="check")
