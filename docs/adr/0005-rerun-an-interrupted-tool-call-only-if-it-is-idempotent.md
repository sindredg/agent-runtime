# 0005: Rerun an interrupted tool call only if the tool is idempotent, otherwise ask a person

Status: accepted, 2026-11-13

## Context

A worker writes a `tool_intent` step before it runs a tool and a `tool_result`
step after. When a worker stops in between, the next worker finds an intent
without a result. The tool may not have started, may have stopped half way, or
may have finished without its result being stored. The log cannot tell which.
For a tool that reads a file this does not matter. For one that sends, pays or
deletes, running it again can do the damage twice.

## Options

| Option | For | Against |
|---|---|---|
| Always rerun | The job always continues without a person. | A tool with side effects can run twice. |
| Never rerun, and stop the job for a person every time | No call is ever repeated unseen. | Most tool calls are reads. Every worker stop during any tool call would need a person. |
| Each tool declares whether it is idempotent. Rerun those, stop for a person on the rest | Reads continue unattended. A call with side effects is repeated only after a person said so. | The declaration is trusted. A job with such a tool can sit waiting for a person. |
| Give each call an idempotency key and let the tool or the system behind it refuse a repeat | The repeat becomes safe, with no person involved. | Works only where the system behind the tool supports such keys. It cannot be the general rule. |

## Decision

The third option.

- A tool carries an `idempotent` flag. It is false unless the tool says
  otherwise.
- On takeover, a `model_request` without a `model_response` is sent again.
  Whatever arrived of the first response is discarded.
- A `tool_intent` without a `tool_result` for an idempotent tool is run again
  under a new intent step.
- For any other tool the worker writes an `approval_requested` step that names
  the call, and the job moves to `needs_approval`. Nothing runs.
- A person approves through the API or the CLI. That writes an
  `approval_given` step and moves the job to `queued`. The next worker runs
  the call again under a new intent step.

Every run of a tool has its own intent step. A tool's runs can therefore never
outnumber its intent steps, and an intent that follows an `approval_given`
step is an approved rerun.

Tool calls of one model response run one after another. Only the interrupted
call needs approval. The calls after it have not started.

## Consequences

- Approval means "run it again, knowing it may already have run". The person
  has to check the outside world first. The runtime cannot.
- A job that uses a tool with side effects can stop after any worker failure
  and wait. Nothing times the wait out yet.
- Approve offers a rerun only. Entering the result by hand, skipping the call,
  and cancelling the job are not built.
- A tool that is wrongly declared idempotent is repeated without a question.
- A model call that is sent again is paid for twice. The tokens of the lost
  response are not counted, because its usage never arrived.
- One case is not covered: a worker that is paused after it wrote the intent,
  and wakes after its lease has gone, can still run the tool once
  ([ADR 0004](0004-fence-writes-with-the-lease-epoch-and-a-row-lock.md)).
- At the time of this record none of this is built. The proof is planned:
  workers are killed at random points and at each named crash point while a
  job calls a test tool that keeps its own log of runs. That log must never
  hold more runs than the step log holds intents. The test has to fail when
  the intent write is skipped and when every tool is treated as idempotent.
