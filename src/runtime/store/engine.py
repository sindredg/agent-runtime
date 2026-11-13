"""Connecting to Postgres."""

from sqlalchemy import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def sqlalchemy_url(database_url: str) -> URL:
    """`database_url` with the asyncpg driver named, as SQLAlchemy requires.

    DATABASE_URL is written as plain `postgresql://`. Query parameters are
    passed to asyncpg unchanged, so libpq-only ones such as `sslmode` are not
    supported; asyncpg's name for that setting is `ssl`.
    """
    return make_url(database_url).set(drivername="postgresql+asyncpg")


def create_engine(database_url: str) -> AsyncEngine:
    """An engine with a connection pool. Dispose it when the service stops."""
    return create_async_engine(sqlalchemy_url(database_url))
