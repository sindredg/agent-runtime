# Results: budget, approval and tool output

The evidence for the guarantees about what a job may spend and do. Every
result here is from the run at `3b38cdf`, and each outcome with a mechanism
off from `1f57ee4`, unless a line names another commit. The run's conditions
are on the [Results](README.md) page. What a job may reach is on
[Scope and sandbox](sandbox.md).

The budget, approval and tool-output checks around one round of a job. The
backoff and the repeat stop have their own sections:

```mermaid
flowchart LR
    B{next call fits<br/>the budget?} -->|no| F[job failed]
    B -->|yes| M[model call]
    M --> A{tool call needs<br/>approval?}
    A -->|no| T[tool runs]
    A -->|yes| P[wait for a person]
    P -->|approved| T
    P -->|denied| N[call not run]
    P -->|the deadline| F
    T --> C[output cut to 64 KiB]
    C --> B
    N --> B
```

## Budget, deadline and cancel

Tested. Integration suite, 805 of 805 tests.

- `test_budget.py`: a job that loops, set to make thirty reads of two 2 KB files in turn,
  with a budget of 20,000 tokens.
- It ends `failed` with "the token budget does not cover the next model
  call", after at least 3 and fewer than 30 calls.
- By the fake provider's own count, it spent at most the budget, and that
  count equals the job's `tokens_used`.
- For each call the provider's input stayed within the recorded bound, and
  the count, the bound and `max_tokens` together fit the budget.

Cancel and deadline, in `test_halt_worker.py`, `test_halt_store.py` and the
halt tests of `test_sandbox.py`:

- A cancel in the middle of a model call, and of a sandboxed command, ends
  the job `cancelled`, with the command's container removed.
- The deadline in the middle of each ends it `failed` with an `error` step.
- Each within 5 s, measured from the request or the deadline to the status
  in the database.
- A claim cancels a job whose cancel was requested and fails one past its
  deadline.
- A job waiting for an approval is failed by the workers' timer within 5 s
  of its deadline.

With the mechanism off:

| Removed | Outcome |
|---|---|
| The check that the next call fits | The call was made with no valid `max_tokens`, and the provider refused it. The job failed with that error, not the budget's. |
| The check and the cut of `max_tokens`, at `11b6792` ([run](https://github.com/sindredg/agent-runtime/actions/runs/44808413841)) | The job spent 282,118 tokens against its 20,000, by the provider's count. The test fails on the spend. |
| The cancel check | Both cancel tests fail. The other test ends in the test's wait; the command test ends there or on its status, `succeeded` not `cancelled` |
| The deadline timer | Both deadline tests fail |
| The claim's deadline check | Both cases of the claim's deadline test fail. The job stayed `running`. |
| The claim's cancel, at `11b6792` ([run](https://github.com/sindredg/agent-runtime/actions/runs/44808446584)) | The claim took the job, still `running`, instead of cancelling it. The test fails. |
| The workers' timer, at `11b6792` ([run](https://github.com/sindredg/agent-runtime/actions/runs/44808456953)) | The parked job was never failed. The test fails. |

## Approval before a tool call

Tested. `tests/chaos/test_approval.py`, 200 runs in the chaos run.

- 200 jobs that ask before `create_issue`. A worker holding the job was
  killed at a random time in the first 1.5 s, and again after the answer.
  A kill is sent only when a worker holds the job.
- 238 kills sent, 184 takeovers. 134 jobs were approved and 66 denied. No
  run failed.
- The tracker holds 134 issues for 134 approvals, each created after its
  approval by the tracker's own time, and none for a denied call.
- Worker stopped after the intent of an approved call: a person is asked
  again. Two approvals, one issue.
- Worker stopped after the approved call ran: the check finds the issue. One
  approval, one issue.
- A job nobody answered, with an 8 s timeout: `failed` 0.18 s after its
  deadline, no issue.

`tests/integration/test_approval.py` covers the same with a tool that
records its own runs. It also checks that a spec cannot take a tool out, and
that an answer counts only for the question it names.

With the mechanism off: with the gate removed, all 4 chaos tests fail. In 20
short runs the tracker holds 20 issues for 0 approvals. At `1f57ee4`, 14 of the 19
integration tests failed. The file has 21 tests now; its list entry runs
14 of them ([switched-off runs](switched-off.md#the-entries)).

## A busy provider slows a job

Tested for one job, with one worker. Workers share no limit, so G5's own
line (50 jobs, 4 workers) is not claimed. `test_backoff_in_stack.py`, in the
integration suite.

- The fake answered 429 with `retry-after: 5` for 60 s, from before one
  `ci-triage` job was submitted.
- The job waited in `waiting` with `not_before` set, one backoff step per
  429, and ended `succeeded` with the tool calls of a healthy run.
- No request of the job reached the fake less than 5 s after a 429 to it.
  The fake refused at most 6 of its requests.

With the mechanism off, at `3b38cdf`, each of the three failed its tests: a
429 treated as an error (5), `retry-after` ignored (4), the claim ignoring
`not_before` (5).

## A job that repeats itself

Tested in the integration suite of the run at `3b38cdf`: 4 of 4 tests of
`test_repeat_stop.py` passed, 1 run each, one worker at a time. The stop is described in [Halt](../halt.md#a-job-that-repeats-itself)
and [ADR 0031](../adr/0031-stop-a-job-that-repeats-one-call-with-the-same-result.md).

![Each tool result with the same call and result as the one before adds one to a count. Before the next model call, a count of five fails the job and the call is not sent.](../diagrams/repeat-stop.svg)

- A job that reads one unchanged file ends `failed` after the fifth result.
  The fake provider received 5 model calls: the sixth was never sent.
- A worker that takes over after the fourth result stops the job after the
  fifth: the count comes from the step log.
- Four repeats and then a different result, or the same read of a file that
  changes between reads, do not stop the job.

With the stop off, both stop tests fail on the number of calls the fake
received: [`repeat-stop-off`](switched-off.md#the-entries), from `main`
at `3b38cdf`, 2 of 2.

## Bad tool output

Tested. Integration suite, `test_sandbox.py`.

- One job runs commands that exit non-zero, print 100 MB, and print bytes
  that are not UTF-8. Another runs one past its timeout.
- Each becomes a tool result of at most 64 KiB, the 100 MB one flagged
  `truncated`. Each job succeeds, and the model is sent every result.
- `list_dir`, added at `81750b6`: a listing of 2,000 files and a name that is
  not UTF-8 is cut to 64 KiB and flagged `truncated`. The bad name comes back
  as U+FFFD, and the job succeeds.
- Tools in the worker and their timeouts: [Known limits](../limits.md#tool-calls).

With the mechanism off:

| Removed | Outcome |
|---|---|
| The cut of the stream | The worker kept all 100 MB, and the runner's test fails. The job's test passes: the result is still cut. |
| The cut of the result | The stored result was 65,612 bytes. The job's test fails. |
| The tool timeout | Both timeout tests fail. The command was not stopped. |
| Both cuts, for the `list_dir` test, at `81750b6` | The test fails: the whole listing went to the model, the next call's bound passed the budget, and the job failed. |
