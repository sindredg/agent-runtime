"""Alembic environment: where the migrations run and how they connect.

The database comes from DATABASE_URL, the same variable the services read.
`alembic upgrade head` applies every migration in one transaction, so a
failed migration leaves the schema as it was.
"""

import asyncio
import logging
import os

from alembic import context
from sqlalchemy import URL, Connection, pool
from sqlalchemy.ext.asyncio import create_async_engine

from runtime.store.engine import sqlalchemy_url
from runtime.store.schema import metadata

# Alembic logs each migration it runs at INFO.
logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")


def database_url() -> URL:
    return sqlalchemy_url(os.environ["DATABASE_URL"])


def run_offline() -> None:
    """Print the SQL instead of running it (`alembic upgrade head --sql`)."""
    context.configure(url=database_url(), target_metadata=metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_with(connection: Connection) -> None:
    # target_metadata lets `alembic check` compare the database with the
    # table definitions in runtime.store.schema.
    context.configure(connection=connection, target_metadata=metadata, compare_server_default=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_online() -> None:
    # NullPool: one connection, closed when the migrations are done.
    engine = create_async_engine(database_url(), poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            # Alembic's API is synchronous; run_sync bridges it onto asyncpg.
            await connection.run_sync(run_with)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_offline()
else:
    asyncio.run(run_online())
