# agent-runtime

A platform that runs long AI agent jobs. An agent calls the model, runs a
tool, records the result, and repeats. Every step is written to a log in
Postgres. When a worker goes away in the middle of a job, another worker
continues it from that log.

```mermaid
flowchart LR
    U[runtime CLI] -->|submit, follow| API[api]
    H[a person] -->|approve, deny, cancel| API
    API --> DB[(Postgres:<br/>jobs and the step log)]
    W1[worker] -->|lease and steps| DB
    W2[another worker] -.->|takes over when<br/>the lease runs out| DB
    W1 --> M[model provider]
    W1 --> T[tool call in a container,<br/>network limited to the job's scope]
```

## What is built

- **A job survives its worker.** A worker holds a job through a lease. When
  the lease runs out, another worker resumes the job from the step log.
- **A worker that lost its job cannot write to it.** Its writes are refused.
- **An interrupted tool call runs again only when that is safe.** Otherwise
  the job waits for a person to approve or deny.
- **Shell commands run in a sandbox.** Each call gets a container of its
  own, with no network unless the job has a scope.
- **A scope is enforced by a firewall the command cannot change.** A job
  reaches its targets and nothing else.
- **A tool call can wait for a person** before it runs.
- **A job can be cancelled**, is failed at its deadline, and stays inside its
  token budget.

Not built: retry on provider errors and rate limits. Everything else that is
missing or accepted is in [Known limits](docs/limits.md).

Locally the model provider is a fake that answers from scenario files, so
nothing here calls a paid API.

## Quickstart

Requires [uv](https://docs.astral.sh/uv/), Docker with Compose, make, and
Linux: `make` reads the group of `/var/run/docker.sock` with GNU `stat`.

```sh
git clone https://github.com/sindredg/agent-runtime.git
cd agent-runtime
make up
uv run runtime submit examples/hello.json --follow
```

`make up` builds the images and starts Postgres, the API, one worker, the
fake provider and a fake issue tracker. The job asks the agent to read one
file. The output is the job id, one line per step, and the outcome:

```text
01a1d655-88bd-7468-afc4-5ca3a1947906
   1  model_request   model=hello messages=1
   2  model_response  stop_reason=tool_use tokens=221+44 I will read hello.txt from the workspace.
   3  tool_intent     read_file {"path": "hello.txt"}
   4  tool_result     ok Hello from the workspace.
   5  model_request   model=hello messages=3
   6  model_response  stop_reason=end_turn tokens=314+22 The file hello.txt holds a short greeting. The job is done.
succeeded  tokens_used=601
```

The text of the two responses is scripted in the fake provider. The tool call
and its result are real: the worker read the file.

Next: [the other examples, job files and the CLI](docs/usage.md). `make
clean` stops the stack and deletes its database.

## Results

Each guarantee has a test with a pass line fixed before the run, and a run
with the mechanism switched off to show that the test can fail.

| Guarantee | Status | Evidence |
|---|---|---|
| A job survives a worker stopping | Tested | [200 of 200 jobs, 171 kills](docs/results-durability.md#a-job-survives-a-worker-stopping) |
| An unsafe tool call does not run twice without approval | Tested, one accepted exception | [No operation id with two issues](docs/results-durability.md#an-unsafe-tool-call-does-not-run-twice-without-approval) |
| A stale worker cannot corrupt a job | Tested | [No write after the takeover](docs/results-durability.md#a-stale-worker-cannot-corrupt-a-job) |
| A job that crashes its worker every time does not loop forever | Tested | [Failed after 5 takeovers](docs/results-durability.md#a-job-that-crashes-its-worker-does-not-loop-forever) |
| Every job stays inside its token budget and deadline, and can be cancelled | Tested | [Budget, cancel and deadline](docs/results-controls.md#budget-deadline-and-cancel) |
| A tool can reach only its job's scope, and nothing else of the host | Tested | [22 of 22 escape tests](docs/results-controls.md#scope-and-sandbox) |
| A tool call that needs approval does not run before a person approved it | Tested | [14 issues for 14 approvals](docs/results-controls.md#approval-before-a-tool-call) |
| Bad tool output does not break a job | Built for `run_shell` and `list_dir`, no result recorded | [The test](docs/results-controls.md#bad-tool-output) |
| Provider rate limits slow jobs, they do not fail them | Not built | |
| Throughput scales with workers | Not measured | |

Everything ran on one machine against the fake provider. Where, at which
commit, and what the runs do not show: [Results](docs/results.md).

## Documentation

| Page | Covers |
|---|---|
| [Running jobs](docs/usage.md) | The examples, job files, the CLI |
| [Architecture](docs/architecture.md) | Components, how a job runs, and a map of the other pages |
| [Results](docs/results.md) | The evidence behind each guarantee |
| [Known limits](docs/limits.md) | What is accepted and what is not built |
| [Decision records](docs/adr/README.md) | Why it is built this way |

## Development

uv installs the pinned Python and tools into `.venv`.

| Command | What it does |
|---|---|
| `make up` | Build the images, start every service, wait until all are healthy |
| `make logs` | Follow the service logs |
| `make down` | Stop the services, keep the database volume |
| `make clean` | Stop the services, delete the database volume |
| `make test` | Unit tests |
| `make integration` | Integration tests. Needs the stack from `make up`. |
| `make escape` | Escape tests: tries to get out of the sandbox. Needs no stack. |
| `make chaos` | Chaos tests: kills, pauses and crashes workers. Run `make up` afterwards. |

Suites, failure switches and the lint commands are in
[Testing](docs/testing.md). Ports and environment variables are in
[Settings](docs/settings.md).

The API has no authentication. Do not publish its port on another interface.

## License

Apache License 2.0. See [LICENSE](LICENSE).
