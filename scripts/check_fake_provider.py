"""Exit 1 unless every running worker of the stack is on the fake provider.

`make up-claude` puts the workers on the Claude API. A suite run against that
stack would send its scenario names as model ids, pay for its jobs, and record
a result for the wrong provider. The integration and chaos suites run this
before their first test.

Reads only ANTHROPIC_BASE_URL from each worker, never its whole environment:
the key is in it when the stack is on the Claude API.
"""

import subprocess
import sys

FAKE_PROVIDER = "http://fake-provider:8000"
# The workers of the compose project, found by the labels compose sets.
# `docker ps` needs none of the variables a compose command needs.
WORKERS = [
    "docker",
    "ps",
    "--format",
    "{{.Names}}",
    "--filter",
    "label=com.docker.compose.project=agent-runtime",
    "--filter",
    "label=com.docker.compose.service=worker",
]


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False, timeout=30)  # noqa: S603 - fixed program


def problem() -> str | None:
    """Why the running workers are not all on the fake provider, or None."""
    listed = _run(WORKERS)
    if listed.returncode != 0:
        return f"`docker ps` failed: {listed.stderr.strip()}"
    names = sorted(listed.stdout.split())
    if not names:
        return "no worker is running. Start the stack with `make up`."
    wrong = []
    for name in names:
        url = _run(["docker", "exec", name, "printenv", "ANTHROPIC_BASE_URL"])
        if url.returncode != 0:
            # printenv exits 1 when the variable is unset. The worker then
            # calls the Claude API, the SDK's default.
            wrong.append(f"{name} has no ANTHROPIC_BASE_URL, or did not answer")
        elif url.stdout.strip() != FAKE_PROVIDER:
            wrong.append(f"{name} calls {url.stdout.strip()}")
    if wrong:
        return (
            f"{'; '.join(wrong)}. This suite runs only against the fake provider: "
            "start the stack with `make up`."
        )
    return None


def main() -> int:
    found = problem()
    if found is not None:
        print(f"Not run: {found}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
