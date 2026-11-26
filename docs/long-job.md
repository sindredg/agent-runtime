# Long job

One job is longer than the model's context window and goes on through
[summary steps](adr/0033-summarise-a-long-conversation-as-a-step.md). The
suite in `tests/long` runs it on the fake provider and compares every
request the provider received with the run that nobody interrupted.

Status: **configured, not tested**.

A long job judged by its outcome instead of a reference run is on the
[Long-job grader](long-job-grader.md) page.

![One job runs past its context window with nobody interrupting it and is the reference. A job whose worker is stopped or killed is taken over and must send the same requests. A job whose summary answer is bad, or whose next window opens at the trigger, must end failed; after a bad answer no tool runs.](diagrams/long-job-runs.svg)

## The stack

`docker-compose.long.yml` (`make long-up`) sets up the long stack: two
workers, no sandbox, `MAX_TOKENS` 32000, the default 30 s lease and
six-digit issue numbers. A worker that stops at a failpoint comes back.

- The job's scenario, `long-window`, has a context window of 3,400 of the
  fake's tokens and 44 turns. With `SUMMARY_TRIGGER` 3000 the job stores
  three summaries and sends 47 requests, each inside the window.
- The suite writes its own copy of the scenario with `delay_seconds` 2 on
  every answer. A request then stays open long enough to be seen as the
  job's last step and to kill its worker inside it.
- A wait changes no request, so the sizes are the packaged scenario's. The
  copy keeps the name: the fake's ids carry it, and a longer name would
  move the first summary.
- The suite sets the trigger, the scenarios, the failpoint and the
  tracker's first number on every compose call. None comes from the shell
  or from `.env`.

## The runs

| Run | What happens | Held to |
|---|---|---|
| 1 | The job with nobody interrupting it | Its own clauses below. Every other run is compared with it. |
| 2 | `FAILPOINT=after_summary` stops the first worker | Its last step is the first summary. The requests are run 1's. |
| 3 | `after_model_request` with `FAILPOINT_SUMMARY=1` | Its last step is a summary request it never sent. The requests are run 1's. |
| 4 | Five jobs, one to three kills each, from a fixed seed | The requests are run 1's, resends included. Every kill reached its request. |
| 5 | Six jobs, each with one bad summary answer | The job fails on the stored summary. No tool runs and no issue is opened. |
| 6 | The tracker numbers from 200000, then from 1000000 | Six digits move no summary. A seventh moves the first one a turn earlier. |
| 7 | Every worker killed, new ones with another trigger or none | The requests are those of the trigger the job's first request named. |
| 8 | A trigger equal to the size of a window's first request | The job fails with the error that names the size and the trigger. |

- A planned kill is a pair (k, m): after the job's k-th summary, while its
  m-th request after that is open. The holder is killed with `docker kill`
  and started again; the job waits out its lease and is taken over.
- Run 7 kills every worker, not `docker compose stop`: a worker told to
  stop gives its job back, and the next claim is then no takeover.
- Run 7's third job starts on workers with no trigger and is taken over by
  workers with one. It never summarises and ends as a job with no trigger.

## What a job is checked on

- It ends `succeeded` with exactly three `summary` steps, one in each of
  the first three windows.
- A summary request is the request before it, that request's answer, one
  result for each of its calls, and the fixed instruction.
- Keyed by (window, turn), every request equals run 1's in `system`,
  `tools`, `tool_choice` and `messages`. Only a new issue's number is
  masked, and it must be the tracker's number for that call.
- No key goes back, no key of run 1 is missing, and none is new.
- Each later window opens with the summary as the provider sent it, turn
  marker included, and the continuation line.
- The tracker holds one issue for each call the script makes.
- Every request at the provider was announced by a `model_request` step,
  and each step names the tools its request offered.
- The job asks no person a question, and the provider's log stayed under
  its cap of 10,000 requests.

The suite holds its own copies of the instruction, the continuation line
and the scenario's summary text. The checks are pure functions in
`tests/long/rules.py`, each with a passing and a breaking log in the unit
tests.

An instruction that has to hold for the whole job belongs in the job
file's `system`, which every request carries, summary requests included
([ADR 0033](adr/0033-summarise-a-long-conversation-as-a-step.md)). The
prompt is the first message of the first window only.

## The two stops

- `after_summary` stops the worker when the job's first `summary` step is
  stored. It goes by the number of summaries in the job's log, so it fires
  once in a job, also on a job that was taken over before.
- `FAILPOINT_SUMMARY=1` limits `after_model_request` to a summary request:
  the request step is written and the call is not sent.

The other failure switches are in [Testing](testing.md#failure-switches).

## What a run reports

- Each run checks every clause, also after one failed, and fails with one
  line per failed clause: a fixed text, then what was found.
- A kill that came after its request, or never, is INCONCLUSIVE: a planned
  kill of run 4, with its seed, or the kill before a takeover in run 7. It
  is a finding, and the run is not repeated until it passes. The same
  holds for a failure nobody can explain.
- A run during which the machine's clock was set is `void`.

## Not covered

The suite does not time a halt. A summary call is halted like any model
call ([Halt](halt.md)). The gap for a lost lease or database is in
[OWASP APTS](apts.md#halt-and-approval).

## Runs that have to fail

Five entries of `scripts/switched_off.py` each switch one mechanism off on
the long stack ([Testing](testing.md#must-fail-runs)). None has a recorded
run yet.

| Entry | Switched off | The test has to fail on |
|---|---|---|
| `long-trigger-off` | The summary trigger | Run 1: the last request is refused for the window |
| `long-replay-skips-summary` | Summary steps in a replayed log | Run 2: more than three summaries. Run 4: a request at a key run 1 lacks. |
| `long-summary-cut` | Half of the summary in a window's opening message | Run 1: a window does not open with the summary |
| `long-own-trigger` | The trigger the first request pinned | Run 7: other requests than the first worker's |
| `long-oversize-off` | The stop of a window that opens at the trigger | Run 8: the job is not failed for it |

## Run it

`make long` builds the images, starts the long stack and runs the suite.
It takes about 30 minutes. `make up` afterwards brings the normal stack
back.

- The session stops before the first test unless every worker is on the
  fake provider with the 30 s lease, and no job of an earlier session is
  unfinished. `make clean` removes such jobs.
- Results go to `.long/<time>/results.jsonl`, one line per run with its
  verdict, commit, seed and counts. The workers' logs and the session's
  scenario files are beside it.
- CI runs the suite on every pull request in the job `Long job (fake
  provider)`. It is not a required check.
