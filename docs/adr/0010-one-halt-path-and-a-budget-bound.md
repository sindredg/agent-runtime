# 0010: Halt a job through one path, and bound a model call's tokens before it is sent

Status: accepted, 2026-11-14

## Context

A job has a token budget, a deadline and a `cancel_requested` flag. They are
stored and nothing enforces them. A job can run as long and spend as much as
the model likes, and nobody can stop it.

A worker spends most of a job waiting: for the model, or for a tool. A check
between two steps comes too late for a call that takes minutes. The worker
already gives up a model call for two reasons: it was told to stop, or its
lease is gone.

The budget has a problem of its own. The obvious rule is to cap `max_tokens`
at what is left. That bounds the output. The input is counted by the provider
and is known only when the answer arrives.

## Options

How a worker learns that a job was cancelled:

| Option | For | Against |
|---|---|---|
| Read the flag between steps | No new code path. | A long model call or tool run delays the cancel by its whole length. |
| A watcher beside the job that reads the flag every second | Bounded delay. The same shape as the heartbeat. | One small query per second for each running job. |
| `LISTEN` and `NOTIFY` | No polling. | A notification is lost when the connection drops, so the polling is needed anyway. |

How the budget is kept:

| Option | For | Against |
|---|---|---|
| Cap the output only | Simple. | Input tokens pass the budget. The guarantee would have to be about output. |
| Ask the provider to count the tokens before each call | Exact. | One more request per turn, and one more endpoint for the fake provider to imitate. |
| Refuse a call when what is left is less than an upper bound of its input | Strict, with no extra request. | A job can be stopped a little earlier than an exact count would stop it. |

## Decision

A watcher, and the upper bound.

- There is one halt signal with four reasons: the worker was told to stop,
  the lease is gone, the job was cancelled, the deadline passed. A model call
  in flight is given up for any of them.
- A running tool container is killed when the job was cancelled, the
  deadline passed or the lease is gone. On a stop the tool is still waited
  for, as before
  ([ADR 0006](0006-count-takeovers-apart-from-claims.md)).
- What the job becomes: `cancelled` for a cancel, `failed` with an `error`
  step for the deadline, both through fenced writes. A lost lease drops the
  job and a stop releases it, as before.
- Cancel is a route of the API. A job that no worker holds is cancelled in
  one statement. For a job a worker holds the route sets the flag, and the
  worker's watcher reads it every second.
- The deadline is checked by the database clock. A claim fails a job that is
  past it. The worker gets the time left with the claim and keeps a timer.
  Jobs that wait for a worker or for an approval are failed at their deadline
  by a statement the workers run on a timer.
- Before each model call the worker bounds the call's input: the input and
  output tokens of the previous response, plus the bytes added to the
  conversation since, plus a fixed allowance for what the provider adds
  around tool definitions. For the first call it is the bytes of the request
  plus that allowance. If the bound does not fit into what is left of the
  budget, the job fails without the call. Otherwise `max_tokens` is cut to
  what is left after the bound.

## Consequences

- A cancel or a deadline takes effect within seconds, also in the middle of a
  model call or a tool run.
- A tool that is killed leaves a `tool_intent` without a result. The job is
  final by then and nobody decides about that call again.
- The bound rests on one assumption: a token is never shorter than one byte
  of the text it came from. The contract test against the provider's API has
  to check that the reported input stays under the bound.
- A job stops when the bound does not fit, which can be some tokens before
  the budget is really used up. A large tool result makes that distance
  larger.
- `tokens_used` counts input and output tokens as the provider reports them.
  Tokens the provider reports under other names, for example for a prompt
  cache, are not counted yet.
- A model call that is given up is still paid for up to that point and is not
  counted, as before.
- At the time of this record none of this is built. The proof is planned:
  jobs that loop, overspend and are cancelled in the middle of a stream and
  of a tool. `tokens_used` must never pass the budget, and a job must stop
  within 5 seconds of its deadline or its cancel. Each check has to fail with
  its mechanism removed.
