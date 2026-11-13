# 0002: Use Postgres as the job queue

Status: accepted, 2026-11-13

## Context

Workers have to find jobs to run: new jobs, jobs that are backing off, and
jobs whose worker stopped. Job state and the step log already live in Postgres
([ADR 0001](0001-build-durability-on-postgres.md)). The question is whether
the queue lives there too or in a message broker.

## Options

| Option | For | Against |
|---|---|---|
| Postgres: `SELECT ... FOR UPDATE SKIP LOCKED` on the `jobs` table | No extra service. Claiming a job and changing its state are one transaction, so the queue and the job data cannot disagree. An expired lease on the row is the redelivery mechanism. The queue can be inspected with SQL. | Workers poll. Claims compete with step writes on one database. The throughput ceiling is lower than a broker's and is not measured yet. |
| Amazon SQS | Managed. Visibility timeout and dead-letter queues are built in. | Two systems to keep consistent: a message can exist without its job row, or the reverse, so an outbox or a reconciliation job is needed. Standard queues deliver at least once, so the database still needs fencing. Local runs need an emulator. |
| Redis Streams or RabbitMQ | Push delivery and high throughput. | The same two-system consistency problem, and one more stateful service to run. |

## Decision

Postgres. The `jobs` table is the queue. A job is claimable when it is
`queued`, when it is `waiting` and its `not_before` time has passed, or when
it is `running` and its lease has expired. A worker claims a job with
`SKIP LOCKED`, which lets many workers claim at once without waiting on each
other's row locks. The claim sets `lease_owner` and `lease_expires_at` and
increments `lease_epoch`.

## Consequences

- A queued job waits up to one polling interval before a worker picks it up.
  That is small next to jobs that run for minutes.
- The scale limit is unknown until it is measured. A load test with several
  workers is planned, and its numbers decide in a new record whether the queue
  stays on Postgres.
- The index that serves the claim query is added with the claim query, so it
  can be checked against the query plan.
