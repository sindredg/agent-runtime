"""Connecting to Postgres."""

from sqlalchemy import URL, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


class DatabaseUrlError(ValueError):
    """The database URL cannot be used. The message never repeats the URL:
    it holds a password, and a password that is not URL-safe is the usual cause.
    """


def sqlalchemy_url(database_url: str) -> URL:
    """`database_url` with the asyncpg driver named, as SQLAlchemy requires.

    DATABASE_URL is written as plain `postgresql://`. Query parameters are
    passed to asyncpg unchanged, so libpq-only ones such as `sslmode` are not
    supported; asyncpg's name for that setting is `ssl`.
    """
    try:
        url = make_url(database_url)
    except ValueError, ArgumentError:
        # The parser's text quotes the part it could not read, which is
        # often the tail of the password.
        raise DatabaseUrlError(
            "the database URL does not parse; its password must be URL-safe"
        ) from None
    return url.set(drivername="postgresql+asyncpg")


def create_engine(
    database_url: str, application_name: str | None = None, *, hide_parameters: bool = False
) -> AsyncEngine:
    """An engine with a connection pool. Dispose it when the service stops.

    `application_name` names the service in `pg_stat_activity`, so its
    connections can be told apart from anyone else's. `hide_parameters`
    keeps bound values, such as a password, out of SQLAlchemy's error text.
    """
    connect_args = {}
    if application_name is not None:
        connect_args["server_settings"] = {"application_name": application_name}
    return create_async_engine(
        sqlalchemy_url(database_url), connect_args=connect_args, hide_parameters=hide_parameters
    )
