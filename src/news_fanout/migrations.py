"""Schema bootstrap.

`schema.sql` ships inside the package so a container can apply it without the
repo being present. Every statement is idempotent (`CREATE ... IF NOT EXISTS`,
`ON CONFLICT DO NOTHING`), so applying it repeatedly is safe. A session-level
advisory lock serialises concurrent starters so two replicas booting at once
cannot race on `CREATE TABLE`.
"""

import logging
from importlib import resources

from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

# Arbitrary but stable key, so every process contends on the same lock.
_MIGRATION_LOCK_KEY = 7_314_592_016_884_311


def load_schema_sql() -> str:
    return resources.files("news_fanout").joinpath("schema.sql").read_text(encoding="utf-8")


async def apply_schema(engine: AsyncEngine) -> None:
    """Apply `schema.sql` in one transaction, guarded by an advisory lock.

    The raw asyncpg connection is used deliberately: `Connection.execute` with no
    bound parameters goes through the simple query protocol, which is the only
    way to send a multi-statement script in one round trip.
    """
    sql = load_schema_sql()
    async with engine.connect() as connection:
        driver_connection = (await connection.get_raw_connection()).driver_connection
        async with driver_connection.transaction():
            await driver_connection.execute(f"SELECT pg_advisory_xact_lock({_MIGRATION_LOCK_KEY})")
            await driver_connection.execute(sql)
    logger.info("schema applied")
