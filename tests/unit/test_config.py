"""Settings from environment variables."""

from pathlib import Path

import pytest

from runtime.config import ConfigError, ProviderConfig, WorkerConfig

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


def test_worker_settings_are_read() -> None:
    config = WorkerConfig.from_env(
        {
            **WORKER_ENV,
            "WORKER_ID": "w7",
            "WORKSPACE_ROOT": "/data/ws",
            "POLL_INTERVAL_SECONDS": "0.25",
            "WORKER_ALIVE_FILE": "/run/alive",
        }
    )
    assert config.worker_id == "w7"
    assert config.workspace_root == Path("/data/ws")
    assert config.poll_interval == 0.25
    assert config.alive_file == Path("/run/alive")


def test_worker_needs_a_database_url() -> None:
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        WorkerConfig.from_env(ENV)


@pytest.mark.parametrize("value", ["0", "-1", "soon", "nan", "inf"])
def test_poll_interval_must_be_a_positive_number(value: str) -> None:
    with pytest.raises(ConfigError, match="POLL_INTERVAL_SECONDS"):
        WorkerConfig.from_env({**WORKER_ENV, "POLL_INTERVAL_SECONDS": value})
