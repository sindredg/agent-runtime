# 0011: Ask a person before a risky tool call runs

Status: accepted, 2026-11-14

## Context

The runtime asks a person in one case: a tool call was interrupted and
nobody can tell whether it happened
([ADR 0005](0005-rerun-an-interrupted-tool-call-only-when-it-is-safe.md)).
A call that was never interrupted runs without a question, whatever it does.

Some calls should not run on the model's word alone. A security agent can
ask for a command that changes or breaks its target. APTS asks for a human
decision before an action that cannot be undone (HO-010), and for a safe
default when nobody answers (HO-003).

The existing approval can only say yes. There is no way to refuse, and a job
that waits for an answer waits for ever.

## Options

Who decides that a call needs approval:

| Option | For | Against |
|---|---|---|
| The model, by marking its own call | No configuration. | The model is what the check is about. |
| The tool's definition | Enforced by the runtime. One place per tool. | Coarse: every call of the tool is asked about, the harmless ones too. |
| The job's spec, per tool | The operator decides per engagement. | A spec that could loosen a tool's own rule would be a way around it. |
| Rules on the content of the call, for example on the command line | Fine-grained. | A rule language to design and to get right. A command can be written in many ways. |

## Decision

The tool's definition, and the job's spec to tighten it.

- A tool's definition says whether its calls need approval. A job's spec can
  name further tools that need approval for that job. It cannot remove the
  need from a tool that has it.
- For such a call the worker writes `approval_requested` before any
  `tool_intent` and parks the job in `needs_approval`. Nothing of the call
  has run.
- Approve works as it does today: an `approval_given` step, and the job is
  queued. The next worker writes the intent and runs the call. An approval
  covers one run of one call.
- Deny is new: an `approval_denied` step, and the job is queued. The next
  worker stores an error result for the call that says it was denied, without
  running it, and the job goes on. The model sees the refusal and can do
  something else.
- Deny also answers the question of ADR 0005. A person can refuse the rerun
  of an interrupted call, and the job goes on with an error result for it.
- No answer is a no. A job parked for an approval is failed at its deadline
  ([ADR 0010](0010-one-halt-path-and-a-budget-bound.md)), and the call never
  runs.

## Consequences

- The model cannot run a gated tool by itself, however the call is worded.
- The gate is per tool, not per command. With a shell tool gated, listing a
  directory asks as loudly as deleting one. Rules on the content of a call
  are a later decision.
- An approved call that is interrupted asks again, as an interrupted rerun
  does today. One approval never covers two runs.
- The step log gains a step kind, and with it a migration.
- The API has no authentication, so the log records that a call was approved
  or denied and not by whom. What APTS asks about who may decide (HO-004) is
  a gap.
- At the time of this record none of this is built. The proof is planned:
  jobs that call a gated tool while workers are killed at random times. The
  tool's own record must show no run before its approval, a denied call must
  never run, and the test has to fail with the gate removed.
