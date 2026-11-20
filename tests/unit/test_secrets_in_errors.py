"""No secret, or text typed in the wrong place, is repeated in an error or a repr (issue 129).

Each test puts a marker where a secret could stand and checks that the error
text does not carry it.
"""

import asyncio
from typing import Any

import pytest

from runtime.cli import build_parser
from runtime.config import ApiConfig, ConfigError, ProviderConfig, WorkerConfig
from runtime.store import migrate
from runtime.store.engine import DatabaseUrlError, create_engine, sqlalchemy_url
from runtime.store.roles import wait_for_app_role
from runtime.tools.trackers import _issue

SECRET = "s3cret-marker"  # noqa: S105 - a marker, not a secret
# What a worker needs to start, without a tracker.
WORKER_ENV = {"DATABASE_URL": "x", "MODEL": "m", "ANTHROPIC_API_KEY": "k"}


@pytest.mark.parametrize(
    "value", [f"http://tracker:8000/?token={SECRET}", f"http://tracker:8000/#{SECRET}"]
)
def test_a_refused_tracker_url_is_not_repeated(value: str) -> None:
    with pytest.raises(ConfigError) as refused:
        WorkerConfig.from_env({**WORKER_ENV, "TRACKER_URL": value})
    assert "TRACKER_URL" in str(refused.value)
    assert SECRET not in str(refused.value)


@pytest.mark.parametrize("command", [["status"], ["approve"], ["cancel"]])
def test_a_rejected_job_id_is_not_repeated(
    capsys: pytest.CaptureFixture[str], command: list[str]
) -> None:
    typed = f"postgresql://runtime:{SECRET}@127.0.0.1/runtime"
    extra = ["5"] if command == ["approve"] else []
    with pytest.raises(SystemExit):
        build_parser().parse_args([*command, typed, *extra])
    err = capsys.readouterr().err
    assert "not a job id" in err
    assert SECRET not in err


def test_a_rejected_question_number_is_not_repeated(capsys: pytest.CaptureFixture[str]) -> None:
    job = "01a1d655-88bd-7468-afc4-5ca3a1947906"
    with pytest.raises(SystemExit):
        build_parser().parse_args(["approve", job, SECRET])
    err = capsys.readouterr().err
    assert "not a question number" in err
    assert SECRET not in err


def test_the_configs_leave_the_key_and_the_database_url_out_of_their_repr() -> None:
    provider = ProviderConfig(api_key=SECRET, base_url=None, model="m", max_tokens=1)
    assert SECRET not in repr(provider)
    database_url = f"postgresql://runtime_app:{SECRET}@postgres/runtime"
    worker = WorkerConfig.from_env({**WORKER_ENV, "DATABASE_URL": database_url})
    assert SECRET not in repr(worker)
    api = ApiConfig.from_env({"DATABASE_URL": database_url})
    assert SECRET not in repr(api)


def test_an_engine_can_hide_bound_parameters_from_its_errors() -> None:
    hidden = create_engine("postgresql://u:p@127.0.0.1:9/none", hide_parameters=True)
    shown = create_engine("postgresql://u:p@127.0.0.1:9/none")
    assert hidden.sync_engine.hide_parameters is True
    assert shown.sync_engine.hide_parameters is False


@pytest.mark.parametrize(
    "password", [f"a@{SECRET}:x", f"{SECRET}@x:y", f"x:{SECRET}@y:1z"], ids=["1", "2", "3"]
)
def test_a_database_url_that_does_not_parse_is_not_repeated(password: str) -> None:
    url = f"postgresql://runtime:{password}@postgres:5432/runtime"
    with pytest.raises(DatabaseUrlError) as refused:
        sqlalchemy_url(url)
    assert SECRET not in str(refused.value)
    assert refused.value.__cause__ is None
    assert refused.value.__suppress_context__ is True


def test_a_query_key_the_driver_refuses_ends_the_start_check(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A password such as `x@y?name=v` parses, and asyncpg refuses the key `name`."""
    engine = create_engine(f"postgresql://runtime:x@127.0.0.1:9/runtime?{SECRET}=1")

    async def check() -> Any:
        # Without the fix the check tries again forever: the bound makes that a failure.
        return await asyncio.wait_for(
            wait_for_app_role(engine, asyncio.Event(), retry_seconds=0.01), 5
        )

    with pytest.raises(DatabaseUrlError) as refused:
        asyncio.run(check())
    assert SECRET not in str(refused.value)
    assert SECRET not in caplog.text


def test_a_created_issue_needs_no_title_in_the_trackers_answer() -> None:
    assert _issue({"number": 7}, "number").number == 7


def test_migrate_hides_the_password_it_binds(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class Stop(Exception):
        pass

    def engine(url: str, **options: Any) -> Any:
        seen.update(options)
        raise Stop

    monkeypatch.setattr(migrate, "create_engine", engine)
    with pytest.raises(Stop):
        asyncio.run(migrate._allow_login("postgresql://owner:x@postgres/runtime", SECRET))
    assert seen == {"hide_parameters": True}


@pytest.mark.parametrize(
    "argv",
    [
        ["verify", "--all", f"--database_url=postgresql://runtime:{SECRET}@h/runtime"],
        ["--database-url", f"postgresql://runtime:{SECRET}@h/runtime", "verify", "--all"],
        [f"postgresql://runtime:{SECRET}@h/runtime"],
        ["status", "01a1d655-88bd-7468-afc4-5ca3a1947906", SECRET],
    ],
    ids=["unknown-option", "option-before-command", "url-as-command", "extra-argument"],
)
def test_a_refused_argument_is_not_repeated(
    capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    with pytest.raises(SystemExit) as stopped:
        build_parser().parse_args(argv)
    assert stopped.value.code == 2
    err = capsys.readouterr().err
    assert "usage:" in err
    assert SECRET not in err
