# 0036: Judge a leak first, and require the edited job to finish

Status: accepted, 2026-11-26. Amends
[0034](0034-judge-a-keyed-long-job-entry-by-a-control-and-an-edited-job.md)
in two points. The rest of 0034 holds as written.

![The judge checks seven outcomes from top to bottom and the first that matches is the verdict: an answer of the job found in either job, a runtime failure, no record or a void, a control that did not pass, an edited job that did not run, an edited job that did not fail as listed or stopped before its end, and pass.](../diagrams/long-must-fail-judge.svg)

## Context

ADR 0034 makes a leak of the job's answers a FINDING. Its judge reads the
answers test after a void and a missing record, so a leak beside either
ended INCONCLUSIVE. It also reports R1 and NO CLASS in the edited job
without judging them, so an edited job that stopped early could pass.

## Options

| Option | For | Against |
|---|---|---|
| Keep 0034's order and reports | No change. | A leak can end INCONCLUSIVE. R7 can fire because the job stopped, not because the notices were lost. |
| A leak first, and an early stop in the edited job INCONCLUSIVE | A stop the edit did not cause is not a finding. | The run says nothing, and the next one costs two more jobs. |
| A leak first, and an early stop in the edited job a FINDING | No leak is hidden. R7, R8 and R9 count only on a job graded to its end. | A model's own early stop fails the run. |

## Decision

The third option.

- The answers test is read first, in both jobs. A leak is a FINDING beside
  a void, a missing record or any other result. Runtime failures and
  missing records are listed beside it.
- A leak is the answers test failing on its assert that found an answer,
  even when a teardown error follows. Its other failures, an error alone
  and a test that did not run are judged as before.
- In the edited job, R1 and NO CLASS are a FINDING. The edited job must
  run to its end and be graded on R7, R8 and R9.
- A missed kill in the edited job is reported, not judged, and too few
  takeovers is now reported with it. The control job's PASS already showed
  the kills land.

## Consequences

- The judge's order: a leak, a runtime failure, a missing record, a void,
  the control job, the edited job.
- The judge matches the text of one assert in the answers test. A unit
  test pins that text, so a change to it fails the unit suite.
- A real model that stops early in the edited job makes the run a FINDING.
  Its record shows which stop.
