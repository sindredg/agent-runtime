"""Settings read from environment variables."""

import os
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from runtime.failpoints import NAMES as FAILPOINT_NAMES
from runtime.sandbox.runner import SandboxSettings, call_within


class ConfigError(Exception):
    """A required setting is missing or has a value that cannot be used."""


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "")
    if not value:
        raise ConfigError(f"{name} is not set")
    return value


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    text = env.get(name, "")
    if not text:
        return default
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise ConfigError(f"{name} must be a positive integer, got {text!r}")
    return value


def _positive_float(env: Mapping[str, str], name: str, default: float) -> float:
    text = env.get(name, "")
    if not text:
        return default
    try:
        value = float(text)
    except ValueError:
        value = 0.0
    # `not value > 0` also refuses NaN.
    if not value > 0 or value == float("inf"):
        raise ConfigError(f"{name} must be a positive number, got {text!r}")
    return value


def _http_url(env: Mapping[str, str], name: str) -> str | None:
    """An optional http or https URL, without a trailing slash."""
    value = env.get(name, "")
    if not value:
        return None
    if not value.startswith(("http://", "https://")):
        raise ConfigError(f"{name} must be an http or https URL, got {value!r}")
    return value.rstrip("/")


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """Which provider to call and how.

    ANTHROPIC_BASE_URL picks the provider: unset means the Claude API, and
    the compose stack points it at the fake provider. MODEL is the model for
    jobs that do not name one. No model id is written in the code.
    """

    api_key: str
    base_url: str | None
    model: str
    max_tokens: int

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ProviderConfig:
        env = os.environ if env is None else env
        return cls(
            api_key=_required(env, "ANTHROPIC_API_KEY"),
            base_url=env.get("ANTHROPIC_BASE_URL") or None,
            model=_required(env, "MODEL"),
            max_tokens=_positive_int(env, "MAX_TOKENS", 16_000),
        )


@dataclass(frozen=True, slots=True)
class SandboxConfig:
    """Where the worker starts tool containers, and what they get.

    SANDBOX_IMAGE switches the sandboxed tools on. Unset, they are not offered.
    """

    docker_socket: str
    settings: SandboxSettings
    # Docker networks whose subnets no scope can reach: the runtime's own.
    # The worker adds them, and the guard network's, to the deny list when
    # it starts (ADR 0009).
    deny_networks: tuple[str, ...] = ()

    @property
    def denied_networks(self) -> tuple[str, ...]:
        """The networks whose subnets go into the deny list: the guards' own first.

        The guards' network is in it so that one job's command cannot reach
        another job's guard or command.
        """
        if self.settings.network is None:
            return ()
        return (self.settings.network, *self.deny_networks)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SandboxConfig | None:
        env = os.environ if env is None else env
        image = env.get("SANDBOX_IMAGE", "")
        if not image:
            return None
        guard_image = env.get("GUARD_IMAGE") or None
        network = env.get("SANDBOX_NETWORK") or None
        if (guard_image is None) != (network is None):
            raise ConfigError("GUARD_IMAGE and SANDBOX_NETWORK are set together or not at all")
        deny_networks = tuple(
            name.strip() for name in env.get("SANDBOX_DENY_NETWORKS", "").split(",") if name.strip()
        )
        if guard_image is not None and not deny_networks:
            # A scoped tool on the runtime's own network could reach Postgres.
            raise ConfigError("SANDBOX_DENY_NETWORKS must name the runtime's own networks")
        return cls(
            docker_socket=env.get("DOCKER_SOCKET") or "/var/run/docker.sock",
            settings=SandboxSettings(
                image=image,
                volume=_required(env, "SANDBOX_VOLUME"),
                timeout=_positive_float(env, "SANDBOX_TIMEOUT_SECONDS", 30.0),
                guard_image=guard_image,
                network=network,
            ),
            deny_networks=deny_networks,
        )


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    database_url: str
    provider: ProviderConfig
    # Appears as `lease_owner` on the jobs this worker holds.
    worker_id: str
    # Each job gets the directory `<workspace_root>/<job id>`.
    workspace_root: Path
    # Seconds between two looks for a job when none was found.
    poll_interval: float
    # Touched every few seconds; the container health check reads its age.
    alive_file: Path
    # How long a claim holds a job. A job whose lease ran out is taken over
    # by the next worker that looks for one.
    lease_seconds: float
    # How often the worker renews the lease of the job it is running.
    heartbeat_seconds: float
    # Where the `create_issue` tool opens issues. Unset, the tool is not offered.
    tracker_url: str | None
    # For chaos tests only. The failpoint at which the worker ends its own
    # process, and the tool the tool failpoints are limited to.
    failpoint: str | None
    failpoint_tool: str | None
    # Set when CHAOS_TOOLS=1: the directory the `crash_worker` tool logs its
    # runs to. Unset, that tool is not offered.
    chaos_log_dir: Path | None
    # Unset when SANDBOX_IMAGE is not set: no sandboxed tools then.
    sandbox: SandboxConfig | None
    # How often the worker removes the workspaces of final and unknown jobs.
    sweep_interval: float
    # Seconds the worker gets after SIGTERM before it is killed: compose's
    # `stop_grace_period`, which sets this too. A sandboxed call has to fit.
    stop_grace_seconds: float

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> WorkerConfig:
        env = os.environ if env is None else env
        lease_seconds = _positive_float(env, "LEASE_SECONDS", 30.0)
        heartbeat_seconds = _positive_float(env, "HEARTBEAT_SECONDS", 10.0)
        if heartbeat_seconds >= lease_seconds:
            # A lease that is not renewed before it ends is lost on every job.
            raise ConfigError(
                f"HEARTBEAT_SECONDS ({heartbeat_seconds:g}) must be shorter than"
                f" LEASE_SECONDS ({lease_seconds:g})"
            )
        failpoint = env.get("FAILPOINT") or None
        if failpoint is not None and failpoint not in FAILPOINT_NAMES:
            raise ConfigError(
                f"FAILPOINT must be one of {', '.join(FAILPOINT_NAMES)}, got {failpoint!r}"
            )
        chaos_log_dir = None
        if env.get("CHAOS_TOOLS") == "1":
            chaos_log_dir = Path(_required(env, "CHAOS_LOG_DIR"))
        stop_grace_seconds = _positive_float(env, "STOP_GRACE_SECONDS", 75.0)
        sandbox = SandboxConfig.from_env(env)
        if sandbox is not None and call_within(sandbox.settings) > stop_grace_seconds:
            # A stop would kill the worker in the middle of a call, and an
            # interrupted run_shell call needs a person.
            raise ConfigError(
                f"a sandboxed call can take {call_within(sandbox.settings):g} s"
                f" (SANDBOX_TIMEOUT_SECONDS {sandbox.settings.timeout:g} plus the start and"
                f" Docker requests), more than STOP_GRACE_SECONDS ({stop_grace_seconds:g})"
            )
        return cls(
            database_url=_required(env, "DATABASE_URL"),
            provider=ProviderConfig.from_env(env),
            worker_id=env.get("WORKER_ID") or f"{socket.gethostname()}-{os.getpid()}",
            workspace_root=Path(env.get("WORKSPACE_ROOT") or "/tmp/workspaces"),  # noqa: S108
            poll_interval=_positive_float(env, "POLL_INTERVAL_SECONDS", 1.0),
            alive_file=Path(env.get("WORKER_ALIVE_FILE") or "/tmp/worker.alive"),  # noqa: S108
            lease_seconds=lease_seconds,
            heartbeat_seconds=heartbeat_seconds,
            tracker_url=_http_url(env, "TRACKER_URL"),
            failpoint=failpoint,
            failpoint_tool=env.get("FAILPOINT_TOOL") or None,
            chaos_log_dir=chaos_log_dir,
            sandbox=sandbox,
            sweep_interval=_positive_float(env, "SWEEP_INTERVAL_SECONDS", 300.0),
            stop_grace_seconds=stop_grace_seconds,
        )


@dataclass(frozen=True, slots=True)
class ApiConfig:
    database_url: str
    # Used for a submission that does not give its own.
    default_token_budget: int
    default_timeout_seconds: int
    # The API has no authentication. It listens on localhost unless told otherwise.
    host: str
    port: int

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ApiConfig:
        env = os.environ if env is None else env
        return cls(
            database_url=_required(env, "DATABASE_URL"),
            default_token_budget=_positive_int(env, "DEFAULT_TOKEN_BUDGET", 1_000_000),
            default_timeout_seconds=_positive_int(env, "DEFAULT_TIMEOUT_SECONDS", 3600),
            host=env.get("API_HOST") or "127.0.0.1",
            port=_positive_int(env, "API_PORT", 8000),
        )
