"""Check that Postgres accepts a connection and answers a query.

Run as `python -m runtime.store.ping`. It exits non-zero when the database is
unreachable, so a service that depends on it does not start.
"""

import asyncio
import os

import asyncpg


async def ping(dsn: str) -> None:
    """Connect, run one query, and disconnect. Raises if any step fails."""
    conn = await asyncpg.connect(dsn, timeout=5)
    try:
        await conn.fetchval("SELECT 1")
    finally:
        await conn.close()


def main() -> None:
    asyncio.run(ping(os.environ["DATABASE_URL"]))


if __name__ == "__main__":
    main()
