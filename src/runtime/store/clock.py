"""Time by the database's clock, the clock every step's `created_at` comes from."""

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


async def seconds_until(engine: AsyncEngine, moment: datetime) -> float:
    """Seconds from now to `moment` by the database clock. Negative once it has passed.

    Compared on the database, so a worker whose own clock is off waits as
    long as one whose clock is right.
    """
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT extract(epoch FROM (:moment - clock_timestamp()))"),
            {"moment": moment},
        )
        return float(result.scalar_one())
