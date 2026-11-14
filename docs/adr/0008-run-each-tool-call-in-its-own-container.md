# 0008: Run each tool call in its own container, started by the worker

Status: accepted, 2026-11-14

## Context

Tools run as Python functions in the worker process. That is acceptable for
`read_file`, which the runtime wrote. It is not acceptable for a tool that
runs a command the model wrote: the command would run with the worker's
files, network and database credentials.

Two things have to be decided: what a tool call is isolated in, and how the
worker, itself a container, gets to start that isolation.

## Options

What a tool call runs in:

| Option | For | Against |
|---|---|---|
| The worker process | Nothing to build. | No boundary. |
| One container per job, kept for the life of the job | Fast calls. State survives between calls. | The container outlives any worker and has to be found again after a takeover. State in it is lost when its host is. |
| One container per tool call | Nothing to find again. The only state is the job's workspace directory, which every worker can already reach. | A container start for every call. |

How the worker reaches Docker:

| Option | For | Against |
|---|---|---|
| The Docker socket, mounted into the worker | No new service. | Whoever controls the worker controls the host. |
| A small service that alone holds the socket and only accepts "run this command for this job" | The worker, which handles model output, cannot ask Docker for anything else. | One more service to build, run and test. |
| A rootless Docker daemon for tool containers | No root on the host behind the socket. | A second daemon to set up on every machine and in CI. |

## Decision

One container per tool call, started by the worker through the mounted
socket.

- The worker talks to the Docker Engine API over the unix socket with the
  HTTP client it already has. No Docker client library is added.
- A sandboxed call gets: no network unless the job has a scope
  ([ADR 0009](0009-enforce-the-target-scope-in-the-network-namespace.md)), a
  read-only root file system, no capabilities, `no-new-privileges`, a user
  that is not root, limits on CPU, memory and process count, and one writable
  mount: the job's own directory of the workspace volume.
- Every call has a timeout. Output is cut at 64 KiB and the result says so.
- The worker builds each container from fixed settings. Model output reaches
  only the command to run, never an image, a mount or a flag.
- Tools that are runtime code and need the worker's network, such as
  `create_issue`, stay in the worker.
- A container can outlive the worker that started it. Three things bound
  that. The command runs under its timeout inside the container, so the
  container ends by itself. Each container carries the job id and the lease
  epoch as labels, and a worker that takes a job over removes the job's
  containers from earlier epochs. And it does not decide about the
  interrupted call ([ADR 0005](0005-rerun-an-interrupted-tool-call-only-when-it-is-safe.md))
  before the call's timeout has passed, counted from its `tool_intent` step
  by the database clock, because a container on another host cannot be
  removed from here.

## Consequences

- A flaw that lets an attacker run code in the worker gives them the host.
  The service in the second table is the follow-up that takes the socket
  away from the worker.
- Tool containers share the host's kernel. A kernel flaw is outside what this
  boundary stops. A stronger runtime such as gVisor is a later decision.
- A tool keeps nothing between calls except files in the workspace.
- A job that is taken over during a sandboxed call waits up to that call's
  timeout before anything is decided about it.
- This extends the accepted gap of
  [ADR 0004](0004-fence-writes-with-the-lease-epoch-and-a-row-lock.md): a
  call that started before the takeover can go on until its timeout. It
  cannot start again in that time.
- At the time of this record none of this is built. The proof is planned as
  a set of escape attempts: the network, the cloud metadata address, host
  paths, another job's workspace, the Docker socket, a fork bomb and a memory
  hog. They must all fail in the sandbox and succeed when the container runs
  with Docker's default settings.
- Tried on Docker 29.1.3: a mount of one directory of a named volume
  (`volume-subpath`) shows that job's files and not another job's, with the
  root file system read-only.
