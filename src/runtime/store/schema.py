"""The tables, as SQLAlchemy Core objects for building statements.

The migrations in `migrations/` define the database. These definitions mirror
them. An integration test runs `alembic check`, which fails when tables,
columns, types, nullability, server defaults, or index columns differ. It does
not compare CHECK constraints, which live only in the migrations, or the WHERE
clause of the partial index.
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    Uuid,
    false,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()

# Statuses a job can be claimed from, now or once backoff and lease expiry are
# built. The claim index covers exactly these rows.
CLAIMABLE_STATUSES_SQL = "status IN ('queued', 'waiting', 'running')"

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
    Column("cancel_requested", Boolean, nullable=False, server_default=false()),
    Column("error", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Index(
        "jobs_claim_order",
        "created_at",
        "id",
        postgresql_where=text(CLAIMABLE_STATUSES_SQL),
    ),
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
)
