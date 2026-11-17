"""Chain the step log with a hash, and refuse a change to it (ADR 0013).

Revision: 0007
Revises: 0006

Each step gets `hash`: SHA-256 over the hash of the step before it and this
row. An insert trigger computes it, refuses a step that does not follow the
job's last one, and writes the new head (`head_seq`, `head_hash`) to the job
row. Further triggers refuse UPDATE, DELETE and TRUNCATE on steps.

The app role may not write the head. The insert trigger writes it with the
owner's rights (SECURITY DEFINER). Existing steps are hashed in `seq` order.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | None = None
depends_on: str | None = None

APP_ROLE = "runtime_app"

# The job columns the app role may insert. Every column but the head. Kept
# here and not imported from 0006: a migration does not change after it ran.
JOB_COLUMNS = (
    "id",
    "status",
    "spec",
    "token_budget",
    "tokens_used",
    "deadline",
    "not_before",
    "lease_owner",
    "lease_expires_at",
    "lease_epoch",
    "takeovers",
    "cancel_requested",
    "error",
    "created_at",
)

# The hash of one step. Every field is fixed in width or carries its length,
# so two different rows never give the same input. `created_at` goes in as
# microseconds since the epoch: as text it would depend on the session's
# time zone. `runtime.store.chain` computes the same hash in Python.
STEP_HASH = """
CREATE FUNCTION public.step_hash(
    prev bytea, job_id uuid, seq integer, lease_epoch integer,
    created_at timestamptz, kind text, payload jsonb
) RETURNS bytea
LANGUAGE sql STABLE STRICT
SET search_path = pg_catalog, pg_temp
RETURN sha256(
    prev
    || uuid_send(job_id)
    || int4send(seq)
    || int4send(lease_epoch)
    || int8send((extract(epoch FROM created_at) * 1000000)::bigint)
    || int4send(octet_length(convert_to(kind, 'UTF8')))
    || convert_to(kind, 'UTF8')
    || convert_to(payload::text, 'UTF8')
)
"""

# The insert trigger. It locks the job row as its UPDATE of the head would,
# before it reads the head, so two inserts for one job run one after the
# other. A claim, which locks the row FOR UPDATE, waits for the insert too.
#
# A `seq` at or below the head is let through with a placeholder hash. The
# step with that `seq` exists, so the primary key refuses the row, and
# INSERT ... ON CONFLICT DO NOTHING still works. A `seq` below 1 is refused by
# the `seq` check. A job that does not exist is refused by the foreign key.
STEPS_CHAIN = """
CREATE FUNCTION public.steps_chain() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    h_seq integer;
    h_hash bytea;
BEGIN
    SELECT head_seq, head_hash INTO h_seq, h_hash
      FROM public.jobs WHERE id = NEW.job_id FOR NO KEY UPDATE;
    IF NOT FOUND OR NEW.seq < 1 THEN
        NEW.hash := '\\x'::bytea;
        RETURN NEW;
    END IF;
    IF NEW.seq <= h_seq THEN
        IF NOT EXISTS (
            SELECT FROM public.steps WHERE job_id = NEW.job_id AND seq = NEW.seq
        ) THEN
            RAISE EXCEPTION 'step % of job % is missing below the head %',
                NEW.seq, NEW.job_id, h_seq
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        NEW.hash := '\\x'::bytea;
        RETURN NEW;
    END IF;
    IF NEW.seq <> h_seq + 1 THEN
        RAISE EXCEPTION 'step % of job % does not follow its last step %',
            NEW.seq, NEW.job_id, h_seq
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    NEW.hash := public.step_hash(
        coalesce(h_hash, uuid_send(NEW.job_id)), NEW.job_id, NEW.seq,
        NEW.lease_epoch, NEW.created_at, NEW.kind, NEW.payload
    );
    UPDATE public.jobs SET head_seq = NEW.seq, head_hash = NEW.hash WHERE id = NEW.job_id;
    RETURN NEW;
END
$$
"""

STEPS_REFUSE = """
CREATE FUNCTION public.steps_refuse() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    RAISE EXCEPTION 'the step log is append-only: % refused', TG_OP
        USING ERRCODE = 'integrity_constraint_violation';
END
$$
"""

# The hashes of the steps that exist already, in `seq` order per job. Each
# hash needs the one before it, so this is a loop with one UPDATE per step,
# under the migration's lock on the table.
BACKFILL = """
DO $$
DECLARE
    r record;
    job uuid;
    prev bytea;
BEGIN
    FOR r IN SELECT * FROM public.steps ORDER BY job_id, seq LOOP
        IF job IS DISTINCT FROM r.job_id THEN
            job := r.job_id;
            prev := uuid_send(r.job_id);
        END IF;
        prev := public.step_hash(
            prev, r.job_id, r.seq, r.lease_epoch, r.created_at, r.kind, r.payload
        );
        UPDATE public.steps SET hash = prev WHERE job_id = r.job_id AND seq = r.seq;
    END LOOP;
END
$$
"""

SET_HEADS = """
UPDATE public.jobs AS j
   SET head_seq = s.seq, head_hash = s.hash
  FROM (SELECT DISTINCT ON (job_id) job_id, seq, hash
          FROM public.steps ORDER BY job_id, seq DESC) AS s
 WHERE j.id = s.job_id
"""


def upgrade() -> None:
    op.add_column("steps", sa.Column("hash", sa.LargeBinary()))
    op.add_column(
        "jobs",
        sa.Column("head_seq", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    # NULL until the job has a step: the first step chains from the job id.
    op.add_column("jobs", sa.Column("head_hash", sa.LargeBinary()))

    op.execute(STEP_HASH)
    op.execute(BACKFILL)
    op.execute(SET_HEADS)
    op.alter_column("steps", "hash", nullable=False)

    op.execute(STEPS_CHAIN)
    op.execute(STEPS_REFUSE)
    op.execute(
        "CREATE TRIGGER steps_chain BEFORE INSERT ON steps"
        " FOR EACH ROW EXECUTE FUNCTION steps_chain()"
    )
    op.execute(
        "CREATE TRIGGER steps_no_change BEFORE UPDATE OR DELETE ON steps"
        " FOR EACH ROW EXECUTE FUNCTION steps_refuse()"
    )
    op.execute(
        "CREATE TRIGGER steps_no_truncate BEFORE TRUNCATE ON steps"
        " FOR EACH STATEMENT EXECUTE FUNCTION steps_refuse()"
    )

    # 0006 granted INSERT on the whole table. A job inserted with a head of
    # the app's choosing would start its chain wherever the app wanted.
    op.execute(f"REVOKE INSERT ON jobs FROM {APP_ROLE}")
    op.execute(f"GRANT INSERT ({', '.join(JOB_COLUMNS)}) ON jobs TO {APP_ROLE}")
    # Not granted: UPDATE of `head_seq` and `head_hash`. 0006 granted UPDATE
    # by column, so the new columns are not writable for the app role.


def downgrade() -> None:
    op.execute(f"REVOKE INSERT ({', '.join(JOB_COLUMNS)}) ON jobs FROM {APP_ROLE}")
    op.execute(f"GRANT INSERT ON jobs TO {APP_ROLE}")
    op.execute("DROP TRIGGER steps_no_truncate ON steps")
    op.execute("DROP TRIGGER steps_no_change ON steps")
    op.execute("DROP TRIGGER steps_chain ON steps")
    op.execute("DROP FUNCTION steps_refuse()")
    op.execute("DROP FUNCTION steps_chain()")
    op.execute("DROP FUNCTION step_hash(bytea, uuid, integer, integer, timestamptz, text, jsonb)")
    op.drop_column("jobs", "head_hash")
    op.drop_column("jobs", "head_seq")
    op.drop_column("steps", "hash")
