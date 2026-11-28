# Long-job grader

One [generated access review](access-review.md) runs as one job, longer
than the model's context window. The job is judged by its outcome: its
stored steps, the tracker's issues and what the harness saw. No reference
run is compared. The code is in `tests/long_job/`.

Status: **configured, not tested**. The pass line was fixed on 2026-11-27,
moved on 2026-11-28 with the trigger, and lives in `tests/long_job/rules.py`.
One job on the Claude API was graded FAIL on R7 ([one long job on a real model](results/long-job-real-model.md)).

![The grader checks six outcomes from top to bottom and the first that matches is the verdict: a runtime failure, void, no class, an outcome failure, inconclusive, and pass. Every clause that holds is listed with its count, whatever the verdict.](diagrams/long-job-verdict.svg)

## The clauses

| Clause | Holds when |
|---|---|
| R1 | The job did not succeed and stopped on a refusal, `max_tokens`, its deadline, the takeover cap or the provider-error cap. |
| R2 | `runtime verify` exits 1 for the job. |
| R3 | Full shape only: the text of the stored steps is under its floor, in the model's tokens. |
| R5 | A request offered tools other than `read_file` and `create_issue`, or a call to another tool was not answered `unknown tool`. |
| R6 | An operation id has more issues than one plus its approved reruns. |
| R7 | A counted grant that a notice makes a violation has no well-formed issue. |
| R8 | A counted grant that a notice clears has its id in any title of the job. |
| R9 | An issue placed after the first summary lacks the reference code. |

A runtime failure is also a crash record for the job in a worker's log, or
a job that failed on a runtime error. It wins over a void, which stays
listed. VOID covers a clock step, a tracker restart, a harness error, a
failed job with no request and a spend cap.

- A spend cap's `counts.site` names its form: `spend_cap` for a 429 answer,
  `stream_spend_cap` for one inside a stream.
- On a job that did not succeed, R7, R8 and R9 are still listed. They are
  no evidence about the model.

## Counted grants and titles

- **Counted:** the grant's file was first read after the job's first
  summary. A read is a `tool_result` that is not an error and whose content
  has the file's SHA-256. So a counted fact had to cross a summary.
- **A title holds an id** when the id is a whole token: titles are split at
  every character that is not an ASCII letter or digit. **Well formed:**
  exactly one known grant id.
- **The reference code** counts only as it is, between characters that are
  not ASCII letters or digits.
- **An issue's place** is the seq of its call's first `tool_intent`. Its
  window is the number of summaries before that seq.

## INCONCLUSIVE

When no failure holds, the run must still show enough:

- floors: counted grants a notice makes a violation, counted grants a
  notice clears, windows after the first summary with a well-formed issue,
  and on the short shape the number of summaries;
- three kills: the `after_summary` failpoint stopped a worker exactly once,
  two harness kills landed in different windows, and the job was taken
  over at least three times;
- on the full shape, a count that is there and whole;
- `runtime verify` exits 0 or 1;
- grading did not raise. An error inside grading lists `grading_error` and
  keeps its type name in the record's field of that name. A crash or
  verify's exit 1 still makes it FAIL_RUNTIME, and a void of the run VOID.

## The count

- Counted text: `tool_result` content, response text and tool inputs, and
  summary text, joined in seq order.
- It is cut into chunks at step boundaries, each counted as one user
  message. A step larger than a chunk makes the count partial.
- A chunk's value is its count less the count of a one-character message,
  so the total is never over the text's own.

The grader takes the count as an input and calls no model. The short shape
has no count clause.

## The run on the fake provider

`make long-job` starts the long stack and runs `tests/long_job` on the
fake provider, with the job of the committed public seed's short shape.

1. The harness writes a scenario in which the fake plays a reviewer that
   reads every file and opens one issue for each violation.
2. It starts the workers with the fake's trigger and the `after_summary`
   failpoint, and submits the job.
3. It kills the worker that holds the job twice more, a drawn number of
   steps after a summary, and starts it again.
4. After the job it reads the tracker's issues, runs `runtime verify`,
   checks the tracker and the clock, and grades the job.

It takes about 5 minutes, and `make up` afterwards brings the normal stack
back. CI runs it on every pull request that changes more than `docs/`, in the
job `Long job harness (fake provider)`, which is not a required check.

The run passes when the verdict is PASS, the three kills landed, no
question was asked, the job stored at least six summaries, and none of the
job's answers is in the record, the captured output or the saved logs.

The session stops before any stack call for an unknown provider, mode or
phase, for a token budget in a must-fail run, and for every keyed run that
`rules.refusals` refuses. It stops before the job is submitted unless every
worker calls the run's provider.

## On the Claude API

A run starts only when `scripts/long_job_keyed.py check` finds nothing to
refuse. A `counts` run has 10 jobs, and a short-shape run's jobs together
get at most 15,000,000 tokens.

| Path | What it runs |
|---|---|
| `make long-job-claude LONG_JOB_MODEL=claude-haiku-5-5` | Mode `counts` or `graded` on the long stack with the Claude overlay. |
| `scripts/long_job_keyed.py check` | The run's refusals, before the key or any stack. |
| `scripts/long_job_keyed.py corpus-count` | The tokens of a seed's whole corpus. |
| Entry `long-notices-dropped` | Mode `must-fail`: a control job, then a job whose first summary drops the notices ([ADR 0034](adr/0034-judge-a-keyed-long-job-entry-by-a-control-and-an-edited-job.md), [0036](adr/0036-judge-a-leak-first-and-require-the-edited-job-to-finish.md)). |

- **`counts`** runs short jobs one after another, with no kill, and records
  their counts with no verdict.
- **`graded`** runs one full-shape job with the three kills. After the job
  it counts the stored text with `CLAUDE_API_KEY` from the environment. The
  session is green only on PASS: any other verdict, VOID included, fails it.
- A `graded` run takes only the seed whose [commitment](access-review.md#the-command)
  is in `tests/long_job/commitment.json`. `counts` and `must-fail` refuse that seed.
- A keyed run reads its seed from `LONG_JOB_SEED`, writes no scenario and
  records `seed: private`. Its own answers are looked for in every record,
  output and saved log.
- The workflow job `long-job` prints the records only: no service log, no
  saved worker log, no artifact. A failure is read from its record.
- On the fake provider `counts` runs one job. `graded` has no count, so it
  is INCONCLUSIVE and its session is red.

## What it writes

`.long-job/<time>/` holds the scenario on the fake provider, the workers'
saved logs and `results.jsonl`.

- A graded job's line has the verdict, every clause with its count, the
  floors, the kills, the reported counts and `grading_error`.
- A `counts` session writes a line per job and a line of their sum.
- Every line holds counts and fixed words only: no title, id, code or text
  of the job.

## Limits

- The reviewer on the fake provider is scripted. The run shows the harness,
  the kills and the grader end to end, not how a model reviews.
- The keyed paths are tested with stubs and recorders. The entry ran on the
  Claude API ([result](results/long-job-real-model.md)). The fake's reviewer does not
  follow an edited summary, so on the fake the entry's edited job shows nothing.
