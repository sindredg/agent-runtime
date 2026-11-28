# Results: a job longer than its context window

The evidence that a job goes on past the model's context window through
summary steps, and that a takeover sends the same requests. How it works is
on [Long job](../long-job.md). Every run here is on the fake provider. One
job on a real model has [its own page](long-job-real-model.md).

![One job runs past its context window with nobody interrupting it and is the reference. A job whose worker is stopped or killed is taken over and must send the same requests. A job whose summary answer is bad, or whose next window opens at the trigger, must end failed; after a bad answer no tool runs.](../diagrams/long-job-runs.svg)

## The run

| | |
|---|---|
| Run | CI on `main`, [46065646734](https://github.com/sindredg/agent-runtime/actions/runs/46065646734), 2026-11-27. 1 run of each test. |
| Commit | `20a2582` |
| Jobs | The three `Long job (fake provider, <part>)` jobs of CI. Not required checks. |
| Workers and lease | 2 in the long stack, the default 30 s lease |
| Provider | The fake provider, window 3,400 of its tokens, trigger 3,000. The fake tracker. |
| Seed | `293` for the planned kills of run 4. The other runs draw nothing. |
| Where | GitHub-hosted `ubuntu-26.04` runners, Linux x86_64, 4 CPUs. The logs are public and kept 90 days. |

## Each run

Every run passed its clauses ([what a job is checked on](../long-job.md#what-a-job-is-checked-on)).

| Run | Jobs | Result |
|---|---|---|
| 1, nobody interrupts | 1 | `succeeded`: 3 summaries, 47 requests, 180 steps, 88,551 tokens |
| 2, stop after the first summary | 1 | `succeeded`, 1 takeover. The requests are run 1's. |
| 3, stop inside a summary request | 1 | `succeeded`, 1 takeover. The requests are run 1's. |
| 4, planned kills | 5 | `succeeded`, 12 takeovers. 12 kills planned, 12 reached inside their request. |
| 5, bad summary answers | 6 | `failed` on the stored summary. No tool ran and no issue was opened. |
| 6, other tracker numbers | 2 | `succeeded`. Six digits moved no summary; seven moved the first a turn earlier. |
| 7, workers with another trigger | 4 | 2 taken over `succeeded` with run 1's requests. A job begun with no trigger and its reference were both refused for the window. |
| 8, a window opens at the trigger | 2 | `failed` with the error that names size 293 and the trigger |

Run 1 also ran in each of the other two CI jobs, with the same counts.

## Runs that have to fail

Each of the five mechanisms was switched off in a run from `main`, and the
suite failed on the named assertion: [switched-off runs](switched-off.md#the-long-job).

## What these results do not show

- One run of each test at one commit. Later commits run the same suite in CI.
- No real model. A real model's summaries, refusals and token counts are
  not here, and the suite's sizes are the fake's estimate, not a tokenizer.
- The fake's window is 3,400 tokens, so a job of 47 requests crosses it three
  times. A window of a real model's size is not run here.
- No halt is timed. The gap for a lost lease or database is in
  [OWASP APTS](../apts.md#halt-and-approval).
