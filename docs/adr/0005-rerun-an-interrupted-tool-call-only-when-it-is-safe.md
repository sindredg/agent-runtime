# 0005: Rerun an interrupted tool call only when that is known to be safe, otherwise ask a person

Status: accepted, 2026-11-13

## Context

A worker writes a `tool_intent` step before it runs a tool and a `tool_result`
step after. When a worker stops in between, the next worker finds an intent
without a result. The tool may not have started, may have stopped half way, or
may have finished without its result being stored. The log cannot tell which.
For a tool that reads a file this does not matter. For one that sends, pays or
deletes, running it again can do the damage twice.

Runtimes of this kind commonly run the tool again and leave it to the tool's
author to make that harmless.

## Options

| Option | For | Against |
|---|---|---|
| Always rerun | The job always continues without a person. | A tool with side effects can run twice. |
| Never rerun, and stop the job for a person every time | No call is ever repeated unseen. | Most tool calls are reads. Every worker stop during any tool call would need a person. |
| Each tool declares whether it is idempotent. Rerun those, stop for a person on the rest | Reads continue unattended. A call with side effects is repeated only after a person said so. | Every interrupted call with a side effect waits for a person, also when the system behind the tool could have answered the question. |
| The same, and a tool may also bring a check that asks the system behind it whether the call already happened | A call that is found is not repeated and needs no person. A person is asked only when nobody else can tell. | Works only where that system can be asked. A check that finds nothing proves less than one that finds something. |

## Decision

The fourth option. An interrupted call has three outcomes, not two.

- Every tool call has an operation id that stays the same across reruns. It
  is built from the job id and the model's `tool_use_id`, and the tool
  receives it. A tool that changes something outside should store the id with
  the change, or send it as an idempotency key where the other side supports
  one.
- A tool carries an `idempotent` flag, false unless the tool says otherwise,
  and may carry a `check`. The check takes the operation id and the call's
  input and answers one of: done, with the result; not done; unknown.
- On takeover, a `model_request` without a `model_response` is sent again.
  Whatever arrived of the first response is discarded.
- A `tool_intent` without a `tool_result` is decided in this order:

| The tool | What the worker does |
|---|---|
| is idempotent | Runs it again under a new intent step. |
| has a check that answers done | Writes the `tool_result` from the check, marked as recovered. The tool does not run. |
| has a check that answers not done | Runs it again under a new intent step. |
| has a check that answers unknown, or the check fails | Asks a person. |
| has neither | Asks a person. |

- Asking a person means: the worker writes an `approval_requested` step that
  names the call, and the job moves to `needs_approval`. Nothing runs. A
  person approves through the API or the CLI. That writes an `approval_given`
  step and moves the job to `queued`. The next worker runs the call again
  under a new intent step.

Every run of a tool has its own intent step. A tool's runs can therefore never
outnumber its intent steps, and an intent that follows an `approval_given`
step is an approved rerun.

Tool calls of one model response run one after another. Only the interrupted
call is decided this way. The calls after it have not started.

## Consequences

- "Not done" is the tool author's claim, and it is the weak answer. The other
  system may not show a change it has just made, and a request the stopped
  worker sent may still be on its way. A check that cannot rule those out has
  to answer unknown. The lease has to expire before a takeover, which gives a
  request in flight that long to land, and nothing more.
- A worker that was only paused can still run the tool once after it wakes
  ([ADR 0004](0004-fence-writes-with-the-lease-epoch-and-a-row-lock.md)). If a
  check answered "not done" in the meantime and the call was rerun, the change
  is made twice and no person was asked. An idempotency key on the other side
  closes this. Nothing in the runtime does.
- Approval means "run it again, knowing it may already have run". The person
  has to look at the outside world first.
- A job that uses a tool with side effects and no check can stop after any
  worker failure and wait. Nothing times the wait out yet.
- Approve offers a rerun only. Entering the result by hand, skipping the call,
  and cancelling the job are not built.
- A tool that is wrongly declared idempotent is repeated without a question.
- A model call that is sent again is paid for twice. The tokens of the lost
  response are not counted, because its usage never arrived.
- At the time of this record none of this is built. The proof is planned:
  workers are killed at random points and at each named crash point while a
  job calls a tool that creates a record in a separate service. That service's
  own list of records is the evidence. No operation id may appear in it more
  often than once plus the approved reruns. The test has to fail when the
  intent write is skipped and when every tool is treated as idempotent.
