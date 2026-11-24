# 0033: Summarise a long conversation as a step

Status: accepted, 2026-11-24. Amends
[0028](0028-send-the-whole-conversation-and-fail-past-the-context-window.md):
with no trigger set, 0028 holds as written.

![A response reaches the trigger. After its tool results, the worker asks for a summary with tool_choice none, stores the answer as one summary step, and starts the next window from that step's message alone. A window that opens at or over the trigger fails the job.](../diagrams/summary-window.svg)

## Context

ADR 0028 sends the whole conversation with every call, so a job ends at the
model's context window. Some jobs have to read more text than one window
holds. A takeover must still send what the first worker would have sent, and
the log must show what the model was told.

## Options

| Option | For | Against |
|---|---|---|
| A `summary` step kind, fired by a worker setting | Replay rebuilds each window from the log. A worker that does not know the kind stops. | A migration, and a rule for old workers. |
| A marker in an ordinary `model_response` | No migration. | An old worker would end the job `succeeded` on the summary. |
| A trigger on the budget's byte bound | No new count. | One round of parallel reads fires it. |
| A trigger field in the job's spec | Set per job. | Grows the public job format for an operator's setting. |

## Decision

A `summary` step kind, off unless the worker has `SUMMARY_TRIGGER` set.

- **Pinned in the log.** Every `model_request` names the trigger in force
  (`summary_trigger`, or null) and the tools offered (`tools`). Replay keeps
  the trigger of the job's first request; one without the field means none.
  A worker's own setting applies only to a job with no request yet.
- **When.** A call is due, every tool call of the last response is answered,
  and that response's input plus output tokens are at or over the trigger.
- **The request.** The ordinary call path, with `tool_choice: none`, the
  tools unchanged, and the instruction as a text block after the last
  `tool_result`. Its `model_request` has `summary: true`. The budget's bound
  counts the instruction.
- **The answer.** One `summary` step: the response's fields and `message`,
  the user message that opens the next window. Its text is the answer's text
  blocks joined by a blank line, then a blank line and the continuation line.
  Thinking is not carried over.
- **Usable.** Stop reason `end_turn`, text that is not only white space, and
  no tool call. Anything else is stored with a `problem` and fails the job,
  with no retry.
- **Replay.** The conversation becomes the step's message alone and the
  budget's count starts again. The ids of earlier tool calls stay used.
- **Oversize.** A window whose first response reports input tokens at or
  over the trigger fails the job: "the summary leaves the next window at or
  over the trigger: n input tokens, trigger t".

The two texts are frozen with this record. The instruction:

> Your earlier messages are about to be removed to make room. Write a summary
> that lets you continue this task with nothing else: the task and every
> instruction you were given, the facts you found that later steps need, what
> you have done, and what is left to do. Plain text. Do not call a tool.

The continuation line:

> That was your own summary of this job so far, written before your earlier
> messages were removed. Continue the task from where it stops.

## Consequences

- Migration 0012 adds the kind. Its downgrade refuses while a `summary`
  step exists.
- No worker without this change may run while any worker has
  `SUMMARY_TRIGGER` set. An old worker cannot read a `summary` step and
  burns a takeover on every claim.
- A summary can drop what a later step needs. The fake provider cannot show
  that, and no real-model run has.
- Text a job read can come back in its summary, inside a user message instead
  of a tool result. Scope and approvals still hold per call. What a tool
  result can change is stated and tested under issue 160.
- Replay rebuilds the opening message from the stored text. A step whose
  message differs fails the job.
- That the Claude API accepts the request's shape rests on its documentation.
  An entry in the [contract test](../contract-test.md) checks it; it has not been run.
- The summary call costs tokens and counts in `tokens_used`.
