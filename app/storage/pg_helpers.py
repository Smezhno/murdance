"""PostgreSQL migration runner and pool stats helpers.

Separated from postgres.py to keep that module under 300 lines.
Called from PostgresStorage.connect() on app startup.
"""

import logging
from pathlib import Path

import asyncpg

logger = logging.getLogger(__name__)

# Directory that holds numbered .sql migration files.
MIGRATIONS_DIR = Path(__file__).parent / "migrations"


async def run_migrations(pool: asyncpg.Pool) -> None:
    """Execute all .sql files in migrations/ in lexicographic order.

    Each file is run inside a single transaction so a partial failure
    leaves the database unchanged for that migration.

    Files already applied are tracked in the _migrations table so this
    function is safe to call on every startup (idempotent).

    RFC-002 §5.1 Step 0-1: SQL migrations run before the app serves traffic.
    """
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS _migrations (
                filename   TEXT PRIMARY KEY,
                applied_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
            )
        """)

        applied: set[str] = {
            row["filename"]
            for row in await conn.fetch("SELECT filename FROM _migrations")
        }

        migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        if not migration_files:
            logger.warning("pg_migrations: no .sql files found in %s", MIGRATIONS_DIR)
            return

        for path in migration_files:
            if path.name in applied:
                logger.debug("pg_migrations: skip %s (already applied)", path.name)
                continue

            sql = path.read_text(encoding="utf-8")
            logger.info("pg_migrations: applying %s", path.name)

            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO _migrations (filename) VALUES ($1)",
                    path.name,
                )

            logger.info("pg_migrations: applied %s", path.name)


def pool_stats(pool: asyncpg.Pool) -> dict:
    """Return connection pool metrics for the /health endpoint.

    RFC-002 §8: monitor PG connection exhaustion (pool max=10).
    """
    return {
        "pool_min_size": pool.get_min_size(),
        "pool_max_size": pool.get_max_size(),
        "pool_size": pool.get_size(),
        "pool_free": pool.get_idle_size(),
        "pool_used": pool.get_size() - pool.get_idle_size(),
    }


