"""Settings read from environment variables."""

import os
from collections.abc import Mapping
from dataclasses import dataclass


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
