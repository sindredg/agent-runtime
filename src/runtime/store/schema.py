"""The tables, as SQLAlchemy Core objects for building statements.

The migrations in `migrations/` define the database. These definitions mirror
them. An integration test runs `alembic check`, which fails when tables,
columns, types, nullability, server defaults, or index columns differ. It does
not compare CHECK constraints, which live only in the migrations, or the WHERE
clauses of the partial indexes.
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Table,
    Text,
    Uuid,
    false,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()

# Statuses a job can be claimed from: `queued`, `running` once its lease has
# expired, and `waiting` once backoff is built. The claim index covers exactly
# these rows.
CLAIMABLE_STATUSES_SQL = "status IN ('queued', 'waiting', 'running')"
# Jobs that are not final: the ones whose deadline the workers watch.
OPEN_STATUSES_SQL = "status IN ('queued', 'waiting', 'needs_approval', 'running')"

jobs = Table(
    "jobs",
    metadata,
    Column("id", Uuid, primary_key=True, server_default=text("uuidv7()")),
    Column("status", Text, nullable=False, server_default="queued"),
    Column("spec", JSONB, nullable=False),
    Column("token_budget", BigInteger, nullable=False),
    Column("tokens_used", BigInteger, nullable=False, server_default=text("0")),
    Column("deadline", DateTime(timezone=True), nullable=False),
    Column("not_before", DateTime(timezone=True)),
    Column("lease_owner", Text),
    Column("lease_expires_at", DateTime(timezone=True)),
    Column("lease_epoch", Integer, nullable=False, server_default=text("0")),
    Column("takeovers", Integer, nullable=False, server_default=text("0")),
    Column("cancel_requested", Boolean, nullable=False, server_default=false()),
    Column("error", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    # The head of the step log's hash chain: the last step's `seq` and hash.
    # Written only by the insert trigger on steps (ADR 0013).
    Column("head_seq", Integer, nullable=False, server_default=text("0")),
    Column("head_hash", LargeBinary),
    Index(
        "jobs_claim_order",
        "created_at",
        "id",
        postgresql_where=text(CLAIMABLE_STATUSES_SQL),
    ),
    Index("jobs_open_deadline", "deadline", postgresql_where=text(OPEN_STATUSES_SQL)),
    # The job list with a status filter (`runtime.store.listing`).
    Index("jobs_status_id", "status", "id"),
)

steps = Table(
    "steps",
    metadata,
    Column("job_id", Uuid, ForeignKey("jobs.id", name="steps_job_id_fkey"), primary_key=True),
    Column("seq", Integer, primary_key=True, autoincrement=False),
    Column("kind", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("lease_epoch", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    # Set by the insert trigger, whatever the writer sends (ADR 0013).
    Column("hash", LargeBinary, nullable=False),
)

# A question to deliver to the operator's receiver (ADR 0029). The park
# writes the row; the workers send it.
notifications = Table(
    "notifications",
    metadata,
    Column("job_id", Uuid, primary_key=True),
    # The `seq` of the question's `approval_requested` step.
    Column("question", Integer, primary_key=True, autoincrement=False),
    # `before_run` or `interrupted`, never the question's reason text.
    Column("reason", Text, nullable=False),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("next_try", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    # Set by a 2xx only.
    Column("delivered_at", DateTime(timezone=True)),
    # Set by a 2xx, or when the question was answered or the job ended first.
    Column("closed_at", DateTime(timezone=True)),
    ForeignKeyConstraint(
        ["job_id", "question"],
        ["steps.job_id", "steps.seq"],
        name="notifications_question_fkey",
    ),
    Index("notifications_due", "next_try", postgresql_where=text("closed_at IS NULL")),
)

# The columns a `Job` and a `Step` are read from. The chain's columns are
# read only by `runtime.store.chain`.
CHAIN_COLUMNS = frozenset({"head_seq", "head_hash", "hash"})
job_columns = tuple(column for column in jobs.c if column.name not in CHAIN_COLUMNS)
step_columns = tuple(column for column in steps.c if column.name not in CHAIN_COLUMNS)
