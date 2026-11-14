"""Settings from environment variables."""

from pathlib import Path

import pytest

from runtime.config import ApiConfig, ConfigError, ProviderConfig, WorkerConfig
from runtime.sandbox.runner import SandboxSettings

ENV = {"ANTHROPIC_API_KEY": "k", "MODEL": "some-model"}


def test_defaults() -> None:
    config = ProviderConfig.from_env(ENV)
    assert config == ProviderConfig(
        api_key="k", base_url=None, model="some-model", max_tokens=16_000
    )


def test_base_url_and_max_tokens_are_read() -> None:
    config = ProviderConfig.from_env(
        {**ENV, "ANTHROPIC_BASE_URL": "http://fake:8000", "MAX_TOKENS": "512"}
    )
    assert config.base_url == "http://fake:8000"
    assert config.max_tokens == 512


def test_an_empty_base_url_means_the_default() -> None:
    assert ProviderConfig.from_env({**ENV, "ANTHROPIC_BASE_URL": ""}).base_url is None


@pytest.mark.parametrize("missing", ["ANTHROPIC_API_KEY", "MODEL"])
def test_a_missing_required_setting_is_named(missing: str) -> None:
    env = {name: value for name, value in ENV.items() if name != missing}
    with pytest.raises(ConfigError, match=missing):
        ProviderConfig.from_env(env)


@pytest.mark.parametrize("value", ["0", "-5", "many", "1.5"])
def test_max_tokens_must_be_a_positive_integer(value: str) -> None:
    with pytest.raises(ConfigError, match="MAX_TOKENS"):
        ProviderConfig.from_env({**ENV, "MAX_TOKENS": value})


WORKER_ENV = {**ENV, "DATABASE_URL": "postgresql://u:p@h/db"}


def test_worker_defaults() -> None:
    config = WorkerConfig.from_env(WORKER_ENV)
    assert config.database_url == "postgresql://u:p@h/db"
    assert config.provider == ProviderConfig.from_env(ENV)
    assert config.worker_id
    assert config.workspace_root == Path("/tmp/workspaces")  # noqa: S108
    assert config.poll_interval == 1.0
    assert config.alive_file == Path("/tmp/worker.alive")  # noqa: S108
    assert config.lease_seconds == 30.0
    assert config.heartbeat_seconds == 10.0
    assert config.tracker_url is None
    assert config.sandbox is None
    assert config.sweep_interval == 300.0


def test_worker_settings_are_read() -> None:
    config = WorkerConfig.from_env(
        {
            **WORKER_ENV,
            "WORKER_ID": "w7",
            "WORKSPACE_ROOT": "/data/ws",
            "POLL_INTERVAL_SECONDS": "0.25",
            "WORKER_ALIVE_FILE": "/run/alive",
            "LEASE_SECONDS": "2.5",
            "HEARTBEAT_SECONDS": "0.5",
            "TRACKER_URL": "http://fake-tracker:8000/",
            "SWEEP_INTERVAL_SECONDS": "60",
        }
    )
    assert config.worker_id == "w7"
    assert config.workspace_root == Path("/data/ws")
    assert config.poll_interval == 0.25
    assert config.alive_file == Path("/run/alive")
    assert config.lease_seconds == 2.5
    assert config.heartbeat_seconds == 0.5
    assert config.tracker_url == "http://fake-tracker:8000"
    assert config.sweep_interval == 60.0


def test_worker_needs_a_database_url() -> None:
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        WorkerConfig.from_env(ENV)


@pytest.mark.parametrize("value", ["0", "-1", "soon", "nan", "inf"])
def test_poll_interval_must_be_a_positive_number(value: str) -> None:
    with pytest.raises(ConfigError, match="POLL_INTERVAL_SECONDS"):
        WorkerConfig.from_env({**WORKER_ENV, "POLL_INTERVAL_SECONDS": value})


@pytest.mark.parametrize("value", ["0", "-1", "often", "nan", "inf"])
def test_sweep_interval_must_be_a_positive_number(value: str) -> None:
    with pytest.raises(ConfigError, match="SWEEP_INTERVAL_SECONDS"):
        WorkerConfig.from_env({**WORKER_ENV, "SWEEP_INTERVAL_SECONDS": value})


@pytest.mark.parametrize("value", ["0", "-1", "long", "nan", "inf"])
def test_lease_seconds_must_be_a_positive_number(value: str) -> None:
    with pytest.raises(ConfigError, match="LEASE_SECONDS"):
        WorkerConfig.from_env({**WORKER_ENV, "LEASE_SECONDS": value})


@pytest.mark.parametrize("value", ["0", "-1", "often", "nan", "inf"])
def test_heartbeat_seconds_must_be_a_positive_number(value: str) -> None:
    with pytest.raises(ConfigError, match="HEARTBEAT_SECONDS"):
        WorkerConfig.from_env({**WORKER_ENV, "HEARTBEAT_SECONDS": value})


@pytest.mark.parametrize(
    "env",
    [
        # The default heartbeat of 10 s does not fit a 5 s lease.
        {"LEASE_SECONDS": "5"},
        {"LEASE_SECONDS": "5", "HEARTBEAT_SECONDS": "5"},
        {"HEARTBEAT_SECONDS": "31"},
    ],
)
def test_the_heartbeat_must_be_shorter_than_the_lease(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError, match="shorter than LEASE_SECONDS"):
        WorkerConfig.from_env({**WORKER_ENV, **env})


@pytest.mark.parametrize("value", ["fake-tracker:8000", "ftp://tracker", "file:///etc/passwd"])
def test_the_tracker_url_must_be_http_or_https(value: str) -> None:
    with pytest.raises(ConfigError, match="TRACKER_URL"):
        WorkerConfig.from_env({**WORKER_ENV, "TRACKER_URL": value})


def test_the_sandbox_is_on_when_an_image_is_named() -> None:
    config = WorkerConfig.from_env(
        {**WORKER_ENV, "SANDBOX_IMAGE": "sandbox:dev", "SANDBOX_VOLUME": "workspaces"}
    )
    assert config.sandbox is not None
    assert config.sandbox.docker_socket == "/var/run/docker.sock"
    assert config.sandbox.settings == SandboxSettings(image="sandbox:dev", volume="workspaces")


def test_sandbox_settings_are_read() -> None:
    config = WorkerConfig.from_env(
        {
            **WORKER_ENV,
            "SANDBOX_IMAGE": "sandbox:dev",
            "SANDBOX_VOLUME": "workspaces",
            "DOCKER_SOCKET": "/run/docker.sock",
            "SANDBOX_TIMEOUT_SECONDS": "2.5",
        }
    )
    assert config.sandbox is not None
    assert config.sandbox.docker_socket == "/run/docker.sock"
    assert config.sandbox.settings.timeout == 2.5


def test_the_sandbox_needs_the_workspace_volume() -> None:
    with pytest.raises(ConfigError, match="SANDBOX_VOLUME"):
        WorkerConfig.from_env({**WORKER_ENV, "SANDBOX_IMAGE": "sandbox:dev"})


@pytest.mark.parametrize("value", ["0", "-1", "long", "nan", "inf"])
def test_the_sandbox_timeout_must_be_a_positive_number(value: str) -> None:
    env = {"SANDBOX_IMAGE": "i", "SANDBOX_VOLUME": "v", "SANDBOX_TIMEOUT_SECONDS": value}
    with pytest.raises(ConfigError, match="SANDBOX_TIMEOUT_SECONDS"):
        WorkerConfig.from_env({**WORKER_ENV, **env})


def test_api_defaults_listen_on_localhost() -> None:
    config = ApiConfig.from_env({"DATABASE_URL": "postgresql://u:p@h/db"})
    assert config == ApiConfig(
        database_url="postgresql://u:p@h/db",
        default_token_budget=1_000_000,
        default_timeout_seconds=3600,
        host="127.0.0.1",
        port=8000,
    )


def test_api_settings_are_read() -> None:
    config = ApiConfig.from_env(
        {
            "DATABASE_URL": "postgresql://u:p@h/db",
            "DEFAULT_TOKEN_BUDGET": "500",
            "DEFAULT_TIMEOUT_SECONDS": "60",
            "API_HOST": "0.0.0.0",  # noqa: S104 - a value under test, nothing listens
            "API_PORT": "9000",
        }
    )
    assert (config.default_token_budget, config.default_timeout_seconds) == (500, 60)
    assert (config.host, config.port) == ("0.0.0.0", 9000)  # noqa: S104


def test_api_needs_a_database_url() -> None:
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        ApiConfig.from_env({})
