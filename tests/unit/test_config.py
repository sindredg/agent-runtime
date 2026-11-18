"""Settings from environment variables."""

from pathlib import Path

import pytest

from runtime.config import ApiConfig, ConfigError, ProviderConfig, TrackerConfig, WorkerConfig
from runtime.sandbox.runner import MIB, SandboxSettings

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
    assert config.tracker is None
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
    assert config.tracker == TrackerConfig("fake", "http://fake-tracker:8000")
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


@pytest.mark.parametrize(
    "value",
    [
        "fake-tracker:8000",
        "ftp://tracker",
        "file:///etc/passwd",
        "http://",
        "http://gitea:3000/?x=",
        "http://gitea:3000#",
        "http://user:pw@gitea:3000",
        "http:user:pw@gitea",
        "https:/user:pw@gitea",
        "http://[::1",
    ],
)
def test_the_tracker_url_must_be_a_plain_http_or_https_url(value: str) -> None:
    with pytest.raises(ConfigError, match="TRACKER_URL") as raised:
        WorkerConfig.from_env({**WORKER_ENV, "TRACKER_URL": value})
    # A password in the URL is not repeated.
    assert "pw" not in str(raised.value)


GITEA_ENV = {
    "TRACKER_KIND": "gitea",
    "TRACKER_URL": "http://gitea:3000/",
    "TRACKER_REPO": "lab-admin/shop",
    "TRACKER_TOKEN": "secretTokenValue0123",
}


def test_a_gitea_tracker_is_read_and_its_token_is_not_printed() -> None:
    config = WorkerConfig.from_env({**WORKER_ENV, **GITEA_ENV})
    assert config.tracker == TrackerConfig(
        "gitea", "http://gitea:3000", "lab-admin/shop", "secretTokenValue0123"
    )
    assert "secretTokenValue0123" not in repr(config)


def test_the_tracker_kind_is_fake_by_default_and_needs_a_url_to_offer_the_tool() -> None:
    assert WorkerConfig.from_env({**WORKER_ENV, "TRACKER_KIND": "gitea"}).tracker is None


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"TRACKER_KIND": "jira"}, "TRACKER_KIND"),
        ({"TRACKER_REPO": ""}, "TRACKER_REPO is not set"),
        ({"TRACKER_TOKEN": ""}, "TRACKER_TOKEN is not set"),
        ({"TRACKER_REPO": "shop"}, "owner/name"),
        ({"TRACKER_REPO": "a/b/../../admin"}, "owner/name"),
        ({"TRACKER_REPO": "a/b?x=1"}, "owner/name"),
        ({"TRACKER_REPO": "../.."}, "owner/name"),
        ({"TRACKER_REPO": "lab/.."}, "owner/name"),
        ({"TRACKER_TOKEN": "abc\n"}, "letters and digits"),
        ({"TRACKER_TOKEN": "s3cr3t\u00e9"}, "letters and digits"),
        ({"TRACKER_TOKEN": "a b"}, "letters and digits"),
    ],
)
def test_a_gitea_tracker_needs_a_repo_and_a_token_of_letters_and_digits(
    change: dict[str, str], message: str
) -> None:
    with pytest.raises(ConfigError, match=message) as raised:
        WorkerConfig.from_env({**WORKER_ENV, **GITEA_ENV, **change})
    # A refused token is not repeated in the error.
    if token := change.get("TRACKER_TOKEN"):
        assert token not in str(raised.value)


def test_the_sandbox_is_on_when_an_image_is_named() -> None:
    config = WorkerConfig.from_env(
        {**WORKER_ENV, "SANDBOX_IMAGE": "sandbox:dev", "SANDBOX_VOLUME": "workspaces"}
    )
    assert config.sandbox is not None
    assert config.sandbox.docker_socket == "/var/run/docker.sock"
    assert config.sandbox.settings == SandboxSettings(
        image="sandbox:dev", volume="workspaces", workspace_limit=1024 * MIB, watch_interval=1.0
    )


def test_sandbox_settings_are_read() -> None:
    config = WorkerConfig.from_env(
        {
            **WORKER_ENV,
            "SANDBOX_IMAGE": "sandbox:dev",
            "SANDBOX_VOLUME": "workspaces",
            "DOCKER_SOCKET": "/run/docker.sock",
            "SANDBOX_TIMEOUT_SECONDS": "2.5",
            "SANDBOX_WORKSPACE_LIMIT_MIB": "16",
            "SANDBOX_WORKSPACE_POLL_SECONDS": "0.25",
            "SANDBOX_WORKSPACE_FILE_LIMIT": "500",
        }
    )
    assert config.sandbox is not None
    assert config.sandbox.docker_socket == "/run/docker.sock"
    assert config.sandbox.settings.timeout == 2.5
    assert config.sandbox.settings.workspace_limit == 16 * MIB
    assert config.sandbox.settings.watch_interval == 0.25
    assert config.sandbox.settings.workspace_entries == 500


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SANDBOX_WORKSPACE_LIMIT_MIB", "0"),
        ("SANDBOX_WORKSPACE_LIMIT_MIB", "1.5"),
        ("SANDBOX_WORKSPACE_POLL_SECONDS", "0"),
        ("SANDBOX_WORKSPACE_POLL_SECONDS", "nan"),
        ("SANDBOX_WORKSPACE_FILE_LIMIT", "0"),
    ],
)
def test_the_workspace_limits_and_the_interval_must_be_positive(name: str, value: str) -> None:
    env = {"SANDBOX_IMAGE": "i", "SANDBOX_VOLUME": "v", name: value}
    with pytest.raises(ConfigError, match=name):
        WorkerConfig.from_env({**WORKER_ENV, **env})


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


SANDBOX_ENV = {**WORKER_ENV, "SANDBOX_IMAGE": "sandbox:dev", "SANDBOX_VOLUME": "workspaces"}


def test_a_sandbox_without_a_guard_cannot_scope_a_call() -> None:
    config = WorkerConfig.from_env(SANDBOX_ENV)

    assert config.sandbox is not None
    assert config.sandbox.settings.guard_image is None
    assert config.sandbox.settings.network is None


def test_the_guard_settings_are_read() -> None:
    config = WorkerConfig.from_env(
        {
            **SANDBOX_ENV,
            "GUARD_IMAGE": "guard:dev",
            "SANDBOX_NETWORK": "sandbox-net",
            "SANDBOX_DENY_NETWORKS": "stack-net, other-net",
        }
    )

    assert config.sandbox is not None
    assert config.sandbox.settings.guard_image == "guard:dev"
    assert config.sandbox.settings.network == "sandbox-net"
    assert config.sandbox.deny_networks == ("stack-net", "other-net")


@pytest.mark.parametrize(
    "changes",
    [
        {"GUARD_IMAGE": "guard:dev"},
        {"SANDBOX_NETWORK": "sandbox-net"},
        # The runtime's own network has to be in the deny list.
        {"GUARD_IMAGE": "guard:dev", "SANDBOX_NETWORK": "sandbox-net"},
        {"GUARD_IMAGE": "guard:dev", "SANDBOX_NETWORK": "n", "SANDBOX_DENY_NETWORKS": " , "},
    ],
)
def test_guard_settings_that_do_not_fit_together_are_refused(changes: dict[str, str]) -> None:
    with pytest.raises(ConfigError, match=r"GUARD_IMAGE|SANDBOX_DENY_NETWORKS"):
        WorkerConfig.from_env({**SANDBOX_ENV, **changes})


def test_the_guards_own_network_comes_first_in_the_denied_networks() -> None:
    """So one job's command cannot reach another job's guard on that network."""
    config = WorkerConfig.from_env(
        {
            **SANDBOX_ENV,
            "GUARD_IMAGE": "guard:dev",
            "SANDBOX_NETWORK": "sandbox-net",
            "SANDBOX_DENY_NETWORKS": "stack-net",
        }
    )

    assert config.sandbox is not None
    assert config.sandbox.denied_networks == ("sandbox-net", "stack-net")


def test_a_sandbox_without_guards_denies_no_networks() -> None:
    config = WorkerConfig.from_env(SANDBOX_ENV)

    assert config.sandbox is not None
    assert config.sandbox.denied_networks == ()


def test_the_defaults_fit_a_sandboxed_call_into_the_stop_grace_period() -> None:
    # 15 + 10 + 30 + 2 + 10 = 67, and 5 for the steps around the call: 72 of 75.
    config = WorkerConfig.from_env(SANDBOX_ENV)
    assert config.stop_grace_seconds == 75
    assert config.sandbox is not None


def test_a_sandbox_timeout_whose_call_does_not_fit_the_stop_grace_period_is_refused() -> None:
    # 3 (measuring the workspace) + 15 + 10 + 40 + 2 + 10 = 80, and 5 more.
    env = {**SANDBOX_ENV, "SANDBOX_TIMEOUT_SECONDS": "40"}
    with pytest.raises(ConfigError, match=r"80 s .* 5 s more: more than STOP_GRACE_SECONDS \(75\)"):
        WorkerConfig.from_env(env)


@pytest.mark.parametrize("grace", ["80", "84"])
def test_a_stop_grace_period_that_fits_the_call_but_not_the_steps_around_it_is_refused(
    grace: str,
) -> None:
    env = {**SANDBOX_ENV, "SANDBOX_TIMEOUT_SECONDS": "40", "STOP_GRACE_SECONDS": grace}
    with pytest.raises(ConfigError, match="STOP_GRACE_SECONDS"):
        WorkerConfig.from_env(env)


def test_a_longer_stop_grace_period_lets_a_longer_timeout_in() -> None:
    env = {**SANDBOX_ENV, "SANDBOX_TIMEOUT_SECONDS": "40", "STOP_GRACE_SECONDS": "85"}
    assert WorkerConfig.from_env(env).stop_grace_seconds == 85


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "soon"])
def test_the_stop_grace_period_must_be_a_positive_number(value: str) -> None:
    with pytest.raises(ConfigError, match="STOP_GRACE_SECONDS"):
        WorkerConfig.from_env({**SANDBOX_ENV, "STOP_GRACE_SECONDS": value})


def test_a_worker_without_the_sandbox_has_no_call_to_fit() -> None:
    config = WorkerConfig.from_env({**WORKER_ENV, "STOP_GRACE_SECONDS": "1"})
    assert config.sandbox is None
