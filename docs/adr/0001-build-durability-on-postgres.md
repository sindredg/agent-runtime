# 0001: Build job durability on Postgres instead of adopting a workflow engine

Status: accepted, 2026-11-13

## Context

A job is a loop of model calls and tool calls that runs for minutes or hours.
It has to survive a worker stopping at any point, and it must not repeat a
tool call that is unsafe to repeat. Durable execution is an existing product
category, so building it again needs a reason.

## Options

| Option | For | Against |
|---|---|---|
| Temporal | Mature and widely run. Event-sourced replay, timers, retries and a UI come with it. | A server cluster with its own database to run and upgrade. Workflow code has to be deterministic. Activities are retried at least once by default, so the rule for tools that are unsafe to repeat still has to be built on top. |
| AWS Step Functions | Managed, nothing to operate. | Runs only on AWS, while the test stack runs on one machine with Docker Compose. A standard workflow is limited to 25,000 history events, and an open-ended agent loop fits a state machine definition poorly. |
| A durable execution library on Postgres (DBOS Transact, for example) | One dependency, the same database as the job data. | Leases, fencing and replay become the library's internals. The tool-call rule is still built on top, and failure tests would have to reach into the library to switch a mechanism off. |
| Build it on Postgres | One dependency. The step log is a plain table. Each guarantee comes down to a statement that can be read and tested, such as a step insert that only succeeds for the current lease holder. | Everything a workflow engine provides has to be written and proven here. A bug in this layer loses or duplicates work. No timers, no versioning of running jobs, no UI. |

## Decision

Build it. An append-only step log in Postgres is the source of truth for a
job. A worker holds a lease on the job it runs. When a worker stops, another
one claims the job, rebuilds the conversation from the log, and continues.

Durability of agent jobs is what this project provides, and its central rule
(a tool call that is unsafe to repeat never runs twice without approval) is
tied to the shape of the step log. A workflow engine would sit under that rule
without removing the work, and would add a cluster to operate.

## Consequences

- No durability claim holds until a failure test proves it: workers killed at
  random points and at named crash points, with the mechanism switched off
  once to show the test can fail. At the time of this record only the schema
  exists. The mechanisms and their tests are planned.
- Jobs that are running when a deploy changes the step format are not handled.
- Revisit this decision if the runtime needs child workflows, long timers, or
  versioning of running jobs. Those are the features a workflow engine would
  bring.
