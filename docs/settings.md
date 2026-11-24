# Settings

Every service reads its settings from environment variables
(`src/runtime/config.py`). The compose file sets them for the stack, and
`.env` overrides the stack's defaults. Copy `.env.example` to start.

## From `.env` to a service

```mermaid
flowchart LR
    E[.env] --> C[docker-compose.yml]
    C -->|LEASE_SECONDS, HEARTBEAT_SECONDS, TRACKER_URL, NOTIFY_URL| W[worker]
    C -->|SANDBOX_TIMEOUT_SECONDS, STOP_GRACE_SECONDS| W
    C -->|API_PORT, POSTGRES_PORT, FAKE_*_PORT| H[host ports on 127.0.0.1]
    C -->|POSTGRES_PASSWORD, APP_DATABASE_PASSWORD| DB[(postgres)]
```

- The provider is not in this picture: `make up` always puts the worker on
  the fake. See [The Claude API](#the-claude-api).
- In `.env`, `API_PORT` is the host port the stack publishes the API on.
  Inside the container the API listens on `0.0.0.0:8000`.
- The stack publishes the API on 8000, Postgres on 5432, the fake provider
  on 8001, the fake tracker on 8002 and the fake receiver on 8003, all on
  `127.0.0.1` only.

## The Claude API

The fake provider is the default. The Claude API takes its own command,
and only the [kill runs](kill-runs.md) can be started against it.

```mermaid
flowchart LR
    U[make up] --> F[worker on the fake provider]
    C[make up-claude] -->|adds docker-compose.claude.yml| A[worker on the Claude API]
    K[CLAUDE_API_KEY, CLAUDE_MODEL in .env] --> C
    F --> T[make integration, make chaos]
    A -->|refused before the first test| T
    KC[make kills-claude KILLS_MODEL=...] -->|adds docker-compose.claude.yml| KA[kill runs on the Claude API]
    K --> KC
    G[claude-api environment secret,<br/>Manual runs workflow] --> KC
```

- Only `docker-compose.claude.yml` reads `CLAUDE_API_KEY`, so the key can
  stay in `.env`. `CLAUDE_MODEL` defaults to `claude-haiku-5-5`.
- Recorded runs get the key from the `claude-api` environment of the
  Manual runs workflow, from `main` only, after a reviewer approves
  ([ADR 0020](adr/0020-run-recorded-kill-and-api-runs-in-a-manual-workflow.md)).
- `make kills-claude` has no default model: it does not start without
  `KILLS_MODEL`.
- `make up-claude` ends by printing the worker's provider URL and model.
  A result from that stack names both.
- `scripts/check_fake_provider.py` does the refusing: it reads each
  worker's `ANTHROPIC_BASE_URL`, and never the whole environment.

## Worker: job and provider

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | required | `postgresql://` URL without `sslmode`, also read by `api` and `migrate`. Names the [app role](database-roles.md) for `api` and `worker`. |
| `ANTHROPIC_BASE_URL` | the Claude API | Which provider to call. The stack sets the fake provider. |
| `ANTHROPIC_API_KEY` | required | The provider key. The fake accepts any value. |
| `MODEL` | required | Model for jobs that name none. With the fake provider, a scenario name. |
| `MAX_TOKENS` | 16000 | The most `max_tokens` a model call asks for |
| `LEASE_SECONDS` | 30 | How long a claim or a renewal holds a job |
| `HEARTBEAT_SECONDS` | 10 | Time between two renewals. Shorter than the lease. |
| `POLL_INTERVAL_SECONDS` | 1 | Wait between two looks for a job when none was found |
| `STOP_GRACE_SECONDS` | 75 | Time the worker gets after SIGTERM. Also the stack's `stop_grace_period`. |
| `WORKER_ID` | host name and process id | Stored as `lease_owner` |
| `WORKER_ALIVE_FILE` | `/tmp/worker.alive` | Touched every 5 seconds. The compose health check reads its age. |
| `SUMMARY_TRIGGER` | unset | Input plus output tokens of a response at which the job's conversation is summarised. Unset, a job this worker starts never is. |

- A job's first model call pins its trigger ([ADR 0033](adr/0033-summarise-a-long-conversation-as-a-step.md)).
  While any worker has `SUMMARY_TRIGGER` set, no worker older than the `summary` step may run: each claim burns a takeover.

## Worker: the issue tracker and notifications

| Variable | Default | Meaning |
|---|---|---|
| `NOTIFY_URL` | unset; the stack sets the fake receiver | Where a parked job's question is posted, exactly as given, never logged ([ADR 0029](adr/0029-notify-from-a-row-the-park-writes.md)). Unset, none is posted. |
| `TRACKER_URL` | unset | The tracker `create_issue` talks to. Unset, the tool is not offered. |
| `TRACKER_KIND` | `fake` | `fake` for the fake tracker, `gitea` for Gitea |
| `TRACKER_REPO` | unset | Gitea only: the repository, as `owner/name` |
| `TRACKER_TOKEN` | unset | Gitea only: a token with `write:issue` and `read:user`. The check counts only its user's issues. |

The [lab demo](lab-demo.md) sets these for Gitea. `tests/lab` reads
`LAB_GITEA_PORT` (3000) and `LAB_GITEA_PASSWORD` (Gitea's admin; unset, a new
random one each run) from the shell. Compose reads the port from `.env` too.

## Worker: workspace and sandbox

| Variable | Default | Meaning |
|---|---|---|
| `WORKSPACE_ROOT` | `/tmp/workspaces` | Parent of the job workspaces. The stack sets the shared volume at `/workspaces`. |
| `SWEEP_INTERVAL_SECONDS` | 300 | Time between two sweeps of `WORKSPACE_ROOT` |
| `SANDBOX_IMAGE` | unset | The image of the tool containers. Unset, `run_shell` and `list_dir` are not offered. |
| `SANDBOX_VOLUME` | required with `SANDBOX_IMAGE` | The named volume mounted at `WORKSPACE_ROOT` |
| `SANDBOX_TIMEOUT_SECONDS` | 30 | Time a sandboxed command may run. See [Sandbox](sandbox.md#time-bounds). |
| `SANDBOX_WORKSPACE_LIMIT_MIB` | 1024 | Soft size limit of a job's workspace. See [Sandbox](sandbox.md#workspace-size). |
| `SANDBOX_WORKSPACE_FILE_LIMIT` | 100000 | Files and directories a job's workspace may hold. Bounds each measurement. |
| `SANDBOX_WORKSPACE_POLL_SECONDS` | 1 | Seconds between two measurements of the workspace while a command runs |
| `GUARD_IMAGE` | unset | The image of a scoped call's guard, with nftables. Read only with `SANDBOX_IMAGE`. |
| `SANDBOX_NETWORK` | set with `GUARD_IMAGE` | The Docker network the guards join. Its subnet is in the deny list. |
| `SANDBOX_DENY_NETWORKS` | required with `GUARD_IMAGE` | Comma-separated Docker network names whose subnets no scope can reach |
| `DOCKER_SOCKET` | `/var/run/docker.sock` | The Docker Engine API socket. The stack mounts the host's socket there. |

- A workspace root belongs to one database: a worker removes the workspaces
  of jobs its database does not know.
- Without `GUARD_IMAGE`, a job with a scope gets an error result for each
  `run_shell` call.
- The stack builds `agent-runtime-sandbox:dev` and `agent-runtime-guard:dev`
  and sets both images.
- `DOCKER_GID` is the group of the host's Docker socket, added to the worker.
  `make` sets it. Every compose command needs it.

## API, CLI and the fakes

| Variable | Read by | Default | Meaning |
|---|---|---|---|
| `DEFAULT_TOKEN_BUDGET` | api | 1000000 | For a submission without `token_budget` |
| `DEFAULT_TIMEOUT_SECONDS` | api | 3600 | For a submission without `timeout_seconds` |
| `API_HOST`, `API_PORT` | api | `127.0.0.1`, `8000` | Where the API listens |
| `API_ALLOWED_HOSTS` | api | `localhost,127.0.0.1` | The names a request may use in `Host`. No ports, no wildcards, no IPv6. |
| `APP_DATABASE_PASSWORD` | migrate | required; the stack sets `runtime-app-dev` | Sets the login of `runtime_app`, the role of the API and the worker. In the stack's URLs too, so URL-safe characters only. |
| `RUNTIME_API_URL` | CLI | `http://127.0.0.1:8000` | Where the CLI finds the API |
| `RUNTIME_DATABASE_URL` | `runtime verify` | the stack's `runtime_app` on `127.0.0.1:5432` | The database whose step log it checks. The app role is enough. |
| `FAKE_SCENARIO_DIR` | fake-provider | scenarios in the package | Directory of scenario files. The long stack mounts the host's `LONG_SCENARIO_DIR` there, by default the packaged one. |
| `FAKE_TRACKER_FIRST_ISSUE` | fake-tracker | unset (numbers from 1) | The first issue number. Set, the tracker starts only if the 100,000th has its width, and gives none past it. |

## The kill runs

| Variable | Default | Meaning |
|---|---|---|
| `KILLS_PROVIDER` | `fake` | `fake` or `claude`. The make targets set it. |
| `KILLS_MODEL` | none | The model id, `claude-` then lower-case letters, digits, dots and hyphens. Required on the Claude API, refused on the fake provider. |
| `KILLS_RUNS` | 20 | Jobs with a kill at a random time |
| `KILLS_FAILPOINT_RUNS` | 5 | Jobs per failpoint |
| `KILLS_SEED` | `20261119` | Fixes each kill time as a fraction of the window |
| `KILLS_WINDOW` | measured | The window in seconds, to repeat a recorded run |
| `KILLS_TESTS` | all | A `pytest -k` selection, for a must-fail run |
| `KILLS_JOB_LIMIT` | 40 on the Claude API, none on the fake provider | Jobs one invocation may submit. On the Claude API at most 40. |
| `SWITCHED_OFF` | none | The entry of `scripts/switched_off.py` that `make switched-off` runs |
| `SWITCHED_OFF_PROVIDER` | `fake` | `claude` lets an entry of the Claude API start. Every job then costs money. |

Read by the [kill runs](kill-runs.md) and the
[switched-off runs](testing.md#must-fail-runs), not by a service. A
switched-off run sets the `KILLS_*` variables from its entry and passes on
none it was given.

The switches for failure tests (`FAILPOINT`, `FAILPOINT_TOOL`, `CHAOS_TOOLS`,
`CHAOS_LOG_DIR`) are in [Testing](testing.md#failure-switches).
