# 0032: Seed a job's workspace with up to 8 MiB of files

Status: accepted, 2026-11-24.

## Context

A spec's `files` seed the job's workspace and were capped at 1 MiB, paths
and contents. A job that has to work through more text than one context
window holds needs at least 1.2M tokens of distinct file text. At about 4
bytes per token that is about 4.6 MiB.

## Options

| Option | For | Against |
|---|---|---|
| 4 MiB | A smaller row. | Can fall short of 1.2M tokens. |
| 8 MiB for every job | Covers the need without counting tokens. One constant. | A larger row and a larger `GET /jobs/{id}`. |
| A limit set per job or per operator | Fits each job. | A new API or settings surface for one need. |
| A read-only data set the operator mounts into the workspace | No size in the row. | More to build. The later route for larger input. |

## Decision

`MAX_FILES_BYTES` is 8 MiB, for every job. It still counts paths and
contents. The size does not depend on a token count.

## Consequences

- A job row can hold about 8 MiB of spec, and `GET /jobs/{id}` returns it whole.
- The body of `POST /jobs` is still not capped ([API](../api.md#refusals)).
- A workspace can start at 8 MiB, inside the watcher's default limit of
  1024 MiB ([Sandbox](../sandbox.md#workspace-size)).
- Text at more than 4 bytes per token may still fall short of 1.2M tokens.
