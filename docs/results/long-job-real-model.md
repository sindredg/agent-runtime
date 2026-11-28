# One long job on a real model

One long job was graded on the Claude API with `claude-haiku-5-5`. The runtime
carried it through 20 summaries and 3 takeovers to `succeeded`. The model's
review missed 2 of the 12 violations that a late notice decides, so the run
did not pass and ended as a finding.

![The control job runs the checkout as it is and must pass, or the run is a finding and no edit is written. Then the first summary is edited to drop the notices, and the edited job must fail on R7, R8 and R9 with its floors met. This run stopped after the control job, which did not pass.](../diagrams/long-must-fail.svg)

## The run

| | |
|---|---|
| Run | The `long-job` job of Manual runs, [46132784497](https://github.com/sindredg/agent-runtime/actions/runs/46132784497). 1 run. |
| Commit | `a4022e9` |
| Entry | `long-notices-dropped`, mode `must-fail`: a control job, then an edited job ([ADR 0034](../adr/0034-judge-a-keyed-long-job-entry-by-a-control-and-an-edited-job.md), [0036](../adr/0036-judge-a-leak-first-and-require-the-edited-job-to-finish.md)). |
| Model | `claude-haiku-5-5` on the Claude API, `MAX_TOKENS` 32,000 |
| Job | The short shape: 36 files of 16 KiB and the index. Trigger 32,000 ([ADR 0038](../adr/0038-size-the-long-job-trigger-so-no-request-passes-100k-tokens.md)), token budget 4,000,000. |
| Workers and lease | 2 in the long stack, the default 30 s lease |
| Seed | Private, not the committed public seed |
| Where | GitHub Actions, Linux x86_64, 4 CPUs |

## The runtime's part

The control job ended `succeeded` in 1,334 s, inside its token budget.

| Check | Result |
|---|---|
| Files read | 36 of 36 |
| Summaries | 20 |
| Takeovers | 3: one at the `after_summary` failpoint, two harness kills. Both kills landed. |
| `runtime verify` | Exit 0 |
| Tokens | 2,833,266: 2,519,525 input and 313,741 output, of which 223,954 thinking |
| Largest request | 56,198 input tokens. No request passed 100,000. |
| Refusals and approvals | No refusal. No approval was asked. |

## The outcome

The grader's verdict is FAIL, on R7 with a count of 2
([the clauses](../long-job-grader.md#the-clauses)).

| Clause or count | Result |
|---|---|
| R7, violations a late notice decides | 2 of 12 had no issue |
| R8, grants a notice clears | None of 4 had an issue |
| R9, the reference code | Every issue after the first summary held it |
| Titles | 27 found, 5 missed, 0 false, 0 malformed, 0 repeated |
| File recall | 7 of 7 carried facts used. 22 rereads ([issue 261](https://github.com/sindredg/agent-runtime/issues/261)). |
| Floors | Met: 12 made, 4 cleared, 14 windows, 20 summaries |

The control job did not pass, so the entry wrote no edit and the edited job
did not run. The entry ended as `FINDING: long-notices-dropped`
([issue 243](https://github.com/sindredg/agent-runtime/issues/243)).

No graded run of the full job, 293 files, was attempted.

## Earlier jobs, not graded

Two earlier runs of the entry in mode `counts` sized the trigger: 10 short
jobs each on `claude-haiku-5-5`, trigger 20,000, budget 1,500,000. All 20
ended `failed`, all 10 on `max_tokens` at `7395f47`
([46072515563](https://github.com/sindredg/agent-runtime/actions/runs/46072515563)), and 8 on the budget and 2 on `max_tokens` at
`bbb805a` ([46081719563](https://github.com/sindredg/agent-runtime/actions/runs/46081719563)).

## What it does not show

- One job, not a rate.
- The full job did not run, so the count of its stored text
  ([the count](../long-job-grader.md#the-count)) was not taken.
- A keyed run keeps no step log, so a dropped notice and a missed grant
  cannot be told apart ([issue 264](https://github.com/sindredg/agent-runtime/issues/264)).
  3 of the 20 violations that need no notice were missed too.
- The edited job did not run. R7, R8 and R9 are not shown to fire on a real
  model when the notices are dropped.
- A larger model was not tried
  ([issue 266](https://github.com/sindredg/agent-runtime/issues/266)).
- The seed is private, so a reader cannot rebuild the job. The organisation
  is generated, not a real one.
