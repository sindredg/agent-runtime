"""Settings from environment variables."""

import pytest

from runtime.config import ConfigError, ProviderConfig

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
