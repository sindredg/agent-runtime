# 0012: An answer names the question it answers

Status: accepted, 2026-11-15

## Context

`POST /jobs/{id}/approve` and `/deny` recorded the answer against whatever
question the job waited on when the request arrived
([ADR 0011](0011-ask-a-person-before-a-risky-tool-call.md)). A job asks more
than once, so an answer can land on a question its sender never saw:

1. A client sends an approval again because the response was lost, or two
   people approve the same question a moment apart. By the time the second
   approval arrives, the job has run the call and asks about the next one.
   That second approval lets the next call run.
2. An approved call is interrupted, and the worker that takes over asks
   again about the same call, with the same operation id
   ([ADR 0005](0005-rerun-an-interrupted-tool-call-only-when-it-is-safe.md)).
   A repeated approval of the first question answers the second one. But
   the second question is different: the call may already have run, and it
   asks whether to run it again.

## Options

| Option | For | Against |
|---|---|---|
| Optional id in the request | Old clients keep working. | An answer without the id still lands anywhere, so the gate is only as sound as the most careless client. |
| The call's `operation_id` | Stable, and already in the question. | Does not tell case 2 apart: both questions name the same operation id. |
| The question's own `seq` | Unique per question. Already in the log, so nothing new is stored to ask. | A client has to read the question before it answers. |

## Decision

An answer names its question by the `seq` of the `approval_requested` step.

- The body of approve and deny is `{"question": <seq>}`, required. A
  missing or non-integer value is a 422.
- The API locks the job's row, then checks that the job is in
  `needs_approval` and that its last `approval_requested` step has that
  `seq`. Otherwise it answers 409, says which question the job waits on,
  and writes nothing.
- The answer step keeps `tool_use_id` and `operation_id`, and adds
  `question`.
- Replay counts an answer only for the question it names, and only the
  first one. An answer that names another question answers nothing: the
  job waits on its question again.
- An answer written before this change names no question. Such a denial
  still counts for the call it names: a denial can only keep a call from
  running. Such an approval counts for nothing: the call is decided again as
  if nobody had answered, so the job asks again, or the tool's check
  settles it.
- `runtime approve` and `runtime deny` take the number. The CLI prints it
  with the question.

## Consequences

- A late or repeated answer cannot approve a call its sender did not see.
  Both cases above are integration tests.
- A client must read the question before it answers. An answer cannot be
  prepared in advance, for example to approve whatever a job asks next.
- The API still has no authentication. The number stops mistakes. It does
  not stop someone who can reach the API: they can read the question too.
