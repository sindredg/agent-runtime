"""G8 without a scope: a command in the sandbox reaches nothing of the host.

Each attempt is a shell script that prints ESCAPED and exits 0 if it got out,
and exits non-zero if it was stopped. It runs as a `run_shell` call of a job.
It passes when the tool result is an error and has no ESCAPED in it. After
every attempt, the canary file on the host is unchanged.

With ESCAPE_DOCKER_DEFAULTS=1 (see conftest.py) the same attempts must fail.
"""

import pytest

from .conftest import Host

# Seconds an attempt to connect may take before it counts as stopped.
CONNECT = 5


def connect(address: str, port: int) -> str:
    return f"timeout {CONNECT} bash -c 'exec 3<>/dev/tcp/{address}/{port}' && echo ESCAPED"


def found(name: str) -> str:
    """A script that looks for a file by name everywhere but /proc and /sys."""
    return (
        r"find / \( -path /proc -o -path /sys \) -prune -o"
        f" -name '{name}' -print 2>/dev/null | grep . && echo ESCAPED"
    )


# name -> script. The scripts see the job's own workspace as /workspace.
ATTEMPTS: dict[str, str] = {
    "dns": "getent hosts example.com && echo ESCAPED",
    "tcp out": connect("1.1.1.1", 443),
    "cloud metadata": connect("169.254.169.254", 80),
    "a network interface besides loopback": ("ls /sys/class/net | grep -vx lo && echo ESCAPED"),
    "the docker socket": (
        "for s in /var/run/docker.sock /run/docker.sock; do"
        ' test -e "$s" && { echo "$s"; echo ESCAPED; exit 0; }; done;'
        r" find / \( -path /proc -o -path /sys \) -prune -o -type s -print 2>/dev/null"
        " | grep . && echo ESCAPED"
    ),
    "write to the root file system": "touch /usr/escape && echo ESCAPED",
    "mount a file system": "mount -t tmpfs none /mnt 2>&1 && echo ESCAPED",
    "capabilities": (
        "grep -qE '^CapEff:[[:space:]]+0+$' /proc/self/status && exit 1; echo ESCAPED"
    ),
    "gain privileges": (
        "grep -qE '^NoNewPrivs:[[:space:]]+1$' /proc/self/status && exit 1; echo ESCAPED"
    ),
    "run as root": 'test "$(id -u)" = 0 && echo ESCAPED',
    # Fork 200 processes that stay. The sandbox allows 64 in all. Bounded on
    # purpose: under Docker's defaults there is no limit, and an unbounded
    # fork bomb would hit the machine that runs the must-fail run.
    "fork 200 processes": (
        "perl -e 'my $n = 0; for (1 .. 200) { my $p = fork(); last unless defined $p;"
        ' if ($p == 0) { sleep 60; exit 0 } $n++ } print "forked $n\\n";'
        ' if ($n == 200) { print "ESCAPED\\n"; exit 0 } exit 1\''
    ),
    # 512 MiB in one string. The sandbox has 256 MiB and no swap.
    "use 512 MiB of memory": ('perl -e \'$x = "a" x (512 * 1024 * 1024); print "ESCAPED\\n"\''),
}


@pytest.mark.parametrize("name", ATTEMPTS)
def test_an_escape_attempt_is_stopped_and_is_a_failed_tool_result(host: Host, name: str) -> None:
    before = host.canary.read_text(), host.canary.stat().st_mtime_ns

    result = host.shell(ATTEMPTS[name])
    print(f"\n{name}: {result.content!r}")

    assert "ESCAPED" not in result.content
    assert result.is_error
    assert (host.canary.read_text(), host.canary.stat().st_mtime_ns) == before


@pytest.mark.parametrize("target", ["canary", "other job"])
def test_files_outside_the_jobs_workspace_cannot_be_found(host: Host, target: str) -> None:
    """The host's files and another job's workspace are not in the container at all."""
    path = host.canary if target == "canary" else host.other_secret
    before = path.read_text(), path.stat().st_mtime_ns

    result = host.shell(found(path.name) + "; ls -a /workspace/..")
    print(f"\n{target}: {result.content!r}")

    assert "ESCAPED" not in result.content
    assert path.name not in result.content
    assert (path.read_text(), path.stat().st_mtime_ns) == before


def test_the_jobs_own_workspace_is_reachable(host: Host) -> None:
    """The control: the sandbox is not simply broken. The one mount works."""
    (host.root / str(host.job) / "own.txt").write_text("mine\n")

    result = host.shell("cat own.txt && echo written > out.txt")

    assert result.is_error is False
    assert result.content == "mine\n[exit status 0]"
    assert (host.root / str(host.job) / "out.txt").read_text() == "written\n"
