# 0006: Count takeovers apart from claims, and release the job when a worker is told to stop

Status: accepted, 2026-11-13

## Context

A job can take its worker down every time it runs, for example through a tool
call that exhausts memory. Each time, the lease expires and another worker
takes the job over. Without a limit this never ends.

`lease_epoch` already counts claims, but it counts all of them: the first
claim, a claim after a worker was stopped for a deploy, and, once they are
built, a claim after a backoff and a claim after an approval. A limit on the
epoch would fail healthy long jobs.

## Options

| Option | For | Against |
|---|---|---|
| Limit `lease_epoch` | No schema change. | Counts claims that have nothing to do with a failure. |
| A `takeovers` column, incremented only when a claim takes a `running` job whose lease expired | Counts exactly the case where a worker went away without a word. The limit stays inside the claim statement. | One more column and a migration. |
| Count from the step log | No new column. | The claim would have to read the log, and it could no longer be one statement. |

## Decision

A `takeovers` column on `jobs`.

- A claim of a `running` job whose lease expired is a takeover and increments
  the column. A claim of a `queued` job does not.
- A job that has already been taken over 5 times is not handed out again. The
  claim marks it `failed` and records the reason, in the same transaction. A
  job therefore runs at most 6 times: the first claim and 5 takeovers.
- A worker that receives SIGTERM stops at the next step boundary and releases
  the job: it moves from `running` to `queued` and the lease is cleared. The
  next claim is not a takeover. The release is fenced like every other write
  ([ADR 0004](0004-fence-writes-with-the-lease-epoch-and-a-row-lock.md)).
- At SIGTERM a model call in flight is abandoned at once. A request without a
  response is sent again anyway. A running tool is waited for, because
  abandoning it would leave an intent without a result
  ([ADR 0005](0005-rerun-an-interrupted-tool-call-only-if-it-is-idempotent.md)).

## Consequences

- `running` to `queued` becomes a legal move. It was not one before.
- The count cannot tell a job that kills its worker from a host that keeps
  losing workers for its own reasons. Five such losses fail a healthy job.
- The count never goes down. A job that runs for hours keeps every takeover it
  ever had.
- A worker that is killed because its tool outlasted the stop grace period
  counts as a takeover.
- An abandoned model call is paid for and sent again.
- The limit of 5 is a constant, not a setting.
- At the time of this record none of this is built. The proof is planned: a
  tool that kills its worker on every run, and a job that must be `failed`
  after 5 takeovers. The test has to fail with the limit removed.
