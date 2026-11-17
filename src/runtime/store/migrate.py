"""Bring the database to the newest schema, then let the app role log in.

Run as `python -m runtime.store.migrate` from the directory that holds
`pyproject.toml` and `migrations/`, as the migrate service does. It connects
as the owner (DATABASE_URL) and sets the app role's password from
APP_DATABASE_PASSWORD on every run, so a changed password takes effect on
the next start and an existing database gets the role too.
"""

import asyncio
import os
from pathlib import Path

from alembic import command
from alembic.config import Config

from runtime.store.engine import create_engine
from runtime.store.roles import allow_login


async def _allow_login(database_url: str, password: str) -> None:
    engine = create_engine(database_url)
    try:
        async with engine.begin() as conn:
            await allow_login(conn, password)
    finally:
        await engine.dispose()


def main() -> None:
    password = os.environ.get("APP_DATABASE_PASSWORD", "")
    if not password:
        raise SystemExit("APP_DATABASE_PASSWORD is not set")
    # The migrations read DATABASE_URL themselves (migrations/env.py) and
    # run in one transaction of their own. The login is set after it
    # committed, once the role and its rights exist.
    command.upgrade(Config(toml_file=Path.cwd() / "pyproject.toml"), "head")
    asyncio.run(_allow_login(os.environ["DATABASE_URL"], password))


if __name__ == "__main__":
    main()
