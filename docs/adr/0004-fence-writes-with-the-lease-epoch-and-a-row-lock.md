# 0004: Fence a worker's writes with the lease epoch and a row lock, and accept one gap

Status: accepted, 2026-11-13

## Context

A worker holds a job through a lease. When the lease expires, another worker
claims the job, and the claim increments `lease_epoch`. The first worker may
only have been paused or cut off from the database. When it comes back it
still believes the job is its own, and its writes must not reach the job.

## Options

All three compare the writer's epoch with the job's inside the write. They
differ in whether the job row is locked and in when the write commits. They
were tried against Postgres 18 with two connections, in two orders: a claim
still open when the step insert runs, and a step insert still open when the
claim runs.

| Option | For | Against |
|---|---|---|
| Check the epoch in the insert, no lock | One statement. Step writes take no lock that a claim has to wait for. | Not a fence. Under `READ COMMITTED` the check reads a snapshot and does not see a claim that commits a moment later. In both orders a step with the old epoch was stored under the new holder. In the second order the new holder's own insert also waited on the stale transaction. |
| Check the epoch and lock the job row (`FOR SHARE`), in a transaction with other statements | Correct in both orders. The lock conflicts with a claim's lock, so one of the two waits and then sees the other's result. | A worker that is paused between the statement and its commit keeps the lock. The claim skips locked rows, so nobody can take the job over for as long as the pause lasts. |
| Check the epoch and lock the job row, one statement per write that commits on its own | Correct in the first order. The second order cannot occur: the statement commits when it completes, so a pause of the worker cannot fall between the check and the commit. The lock is held only while Postgres runs the statement. | A step and its token count have to fit in one statement. A claim that looks at a job in the instant a step is written skips it and finds it on the next poll. |

## Decision

The third option. Every write a worker makes to a job it holds is one
statement that locks the job row, requires the job to be `running` under the
worker's own epoch, and is sent outside a transaction block so it commits when
it completes. That covers a step with its token count, a lease renewal, the
final status, and a release. A write that changes no row means the lease is
gone. The worker then drops the job: it writes nothing more, starts no tool
and no model call, and leaves the workspace to the new holder.

The uniqueness of (`job_id`, `seq`) stays as the second line of defence.

The worker also keeps a local deadline for its lease: the time it sent the
claim or the last renewal, on its monotonic clock, plus the lease length. It
checks that deadline immediately before each tool run and each model call.

## Consequences

- Fencing protects the database, not the outside world. A worker that passes
  the local check and is paused right after it, for longer than its lease,
  starts that one tool or model call when it wakes. Its result is refused, but
  the tool ran. This gap is accepted. Closing it would need a fence inside
  every tool and every system a tool talks to.
- A tool that is already running when the lease is lost runs to its end. Tools
  run in the worker process and cannot be stopped from outside. A sandbox
  container per tool call, which can be killed, is planned.
- The local check trusts the worker's monotonic clock. That clock does not
  advance while the whole machine is suspended, so after a suspend the check
  can pass on an expired lease. The fenced write still refuses the result.
- Renewals and steps of one job lock the same row and briefly wait on each
  other.
- At the time of this record none of this is built. A step is stored with its
  writer's epoch and nothing compares it. The proof is planned: pause a worker
  past its lease, let another take the job, wake the first, and require that
  every write of the stale worker is refused. The same test has to fail with
  the epoch check removed.
