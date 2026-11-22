# Results: switched-off runs from the list

Each entry of `scripts/switched_off.py` removes one mechanism, runs the tests
that guard it, and names the assertion each test must fail on
([ADR 0021](../adr/0021-run-switched-off-runs-from-a-reviewed-list.md)).
A test that ends any other way is red.

The list can also say that a test passes, with the reason. Three tests of
`resume-off` kill the worker before any step is complete, so starting over
is the same as resuming.

Status: tested. Every entry of the fake provider ran once, from `main`.

```mermaid
flowchart LR
    S[one start,<br/>suite switched-off-all] --> N[34 entries of the<br/>fake provider, from main]
    N --> J[one job each:<br/>edit, stack, tests]
    J --> V[96 tests judged:<br/>93 failed on their assertion,<br/>3 passed as listed]
```

| | |
|---|---|
| Run | [44913490395](https://github.com/sindredg/agent-runtime/actions/runs/44913490395), 2026-11-21 |
| Commit | `e1656b5` |
| Entries | 34 of 34 ended as the list says. 1 run each. |
| Tests | 96 of 96 ended as the list says: 93 failed on their named assertion, 3 passed as listed. |
| Provider | The fake provider. No job of the run holds a secret. |
| Where | GitHub-hosted `ubuntu-26.04` runners, one job per entry. The logs are public and kept 90 days. |

## The entries

| Entry | Mechanism switched off | Stack | Tests |
|---|---|---|---|
| `budget-off` | The check that the next call fits and the cut of max_tokens | Normal | 1 |
| `resume-off` | The replay of the step log on a claim | Chaos | 6 |
| `unknown-check-reruns` | Asking a person when a check cannot tell (ADR 0005) | Chaos | 1 |
| `claim-cancel-off` | The claim's cancel of a job whose cancel was requested | Normal | 1 |
| `overdue-timer-off` | The workers' timer that fails jobs waiting past their deadline | Normal | 1 |
| `stop-release-off` | The hand-back of a job when its worker is told to stop | Chaos | 1 |
| `takeover-outlast-off` | The removal of an earlier lease's container, and the wait, after a takeover | Chaos | 1 |
| `resume-off-random-kills` | The replay of the step log on a claim, with workers killed at random times | Chaos | 1 |
| `intent-write-off-chaos` | The step that records a tool call's intent before it runs | Chaos | 2 |
| `every-tool-idempotent` | The tools' own idempotency flags: every tool is treated as idempotent | Chaos | 1 |
| `epoch-check-off` | The lease epoch in the fence of a worker's writes | Chaos | 1 |
| `gitea-check-not-done` | The Gitea check of create_issue, which always says not done | Lab | 1 |
| `issue-marker-off` | The marker line with the operation id in the issue body | Lab | 2 |
| `refusal-triggers-off` | The triggers that refuse a change to the step log | Normal | 3 |
| `chain-check-off` | The comparison of each step's hash in verify | Normal | 15 |
| `budget-check-off` | The check that the next call fits the budget; the cut of max_tokens stays | Normal | 1 |
| `claim-deadline-off` | The claim's check of a job's deadline | Normal | 2 |
| `approval-gate-off` | The gate that asks a person before a tool call that needs approval | Normal | 14 |
| `approval-gate-off-chaos` | The gate that asks a person before a tool call, with workers killed | Chaos | 4 |
| `busy-answer-fails` | The wait on a busy answer: a 429 or 529 fails the job | Normal | 5 |
| `retry-after-ignored` | The floor that the provider's retry-after sets on a wait | None | 4 |
| `claim-ignores-not-before` | The claim's wait for a waiting job's not_before | Normal | 5 |
| `stream-cut-off` | The cut of a command's output stream at the limit | Normal | 1 |
| `result-cut-off` | The cut of a tool result to its limit | Normal | 1 |
| `tool-timeout-off` | The timeout of a sandboxed command, in the runner and in the container | Normal | 2 |
| `halt-leaves-the-call-running` | The cancel of a running sandboxed command when its call ends early, as on a halt | Normal | 3 |
| `workspace-watcher-off` | The measurement of the workspace while a command runs | Normal | 3 |
| `workspace-precheck-off` | The measurement of the workspace before a command starts | None | 4 |
| `workspace-entry-cap-off` | The cap on the files and directories one walk counts | None | 2 |
| `workspace-walk-deadline-off` | The deadline of one walk of the workspace | None | 2 |
| `workspace-run-bound-off` | The part of a call's bound kept for the measurement | None | 1 |
| `workspace-timeout-off` | The time box of one measurement of the workspace | None | 1 |
| `workspace-stop-signal-off` | The signal that tells a walk to stop past its time box or on a cancel | None | 2 |
| `workspace-walk-stop-off` | A walk's check of the signal to stop | None | 1 |

## Entries added after that run

Three entries came with [ADR 0025](../adr/0025-a-tool-in-the-worker-names-how-long-its-request-can-arrive.md).
Each ran once from `main` at `fbb293f`, 2026-11-21, on the fake provider: 5
of 5 tests failed on their named assertion.

| Entry | Mechanism switched off | Stack | Tests | Run |
|---|---|---|---|---|
| `takeover-wait-for-a-request-off` | The bound `create_issue` names for its request, which a takeover waits out | Normal | 2 | [44919781490](https://github.com/sindredg/agent-runtime/actions/runs/44919781490) |
| `create-deadline-off` | The one deadline of the request that opens an issue | None | 2 | [44919789084](https://github.com/sindredg/agent-runtime/actions/runs/44919789084) |
| `abortive-close-off` | The reset of a given-up request's connection | None | 1 | [44919796574](https://github.com/sindredg/agent-runtime/actions/runs/44919796574) |

With the bound off, the test with two workers and a stalled tracker ends
with two issues for one call.

Three more came later, each run once from `main` on the fake provider. Each
test failed on its named assertion.

| Entry | Mechanism switched off | Stack | Tests | Run |
|---|---|---|---|---|
| `notify-loop-off` | The loop in every worker that posts a parked job's question | Chaos | 3 | [45032321971](https://github.com/sindredg/agent-runtime/actions/runs/45032321971), `30108d4` |
| `notify-row-off` | The notification row the park writes with the job's move | Chaos | 3 | [45032324614](https://github.com/sindredg/agent-runtime/actions/runs/45032324614), `30108d4` |
| `tool-env-passed-on` | The tool container's environment, which holds nothing of the worker's | Normal | 1 | [45047751328](https://github.com/sindredg/agent-runtime/actions/runs/45047751328), `7096a5c` |

The first two guard the notification ([results](notify.md#with-the-mechanism-off)). With
the third, the secret test finds the provider key and the tracker token in
a tool result and in two answers of the API.

The one entry on the Claude API, `thinking-signature-changed`, has
[its own run](kill-runs.md#with-the-stored-signature-changed).

## What this run does not show

- One run per entry. The entries on the chaos stack kill workers at random
  times, so a second run takes other paths.
- Four switches of the earlier runs have no entry: the takeover cap, the
  cancel check, the deadline timer, and the skip in the workspace walk.
  Their tests end in a timeout or an error, not on an assertion
  ([issue 163](https://github.com/sindredg/agent-runtime/issues/163)).
- Two more have no entry: the deny list without the runtime's networks
  ([sandbox](sandbox.md)) and both cuts for `list_dir`
  ([controls](controls.md#bad-tool-output)).
- An entry shows that its tests can fail. It does not show that they catch
  every way the mechanism can break.
