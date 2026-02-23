"""PostgreSQL cache for Impulse CRM data.

Replaces Redis SET/GET/SCAN with the crm_cache table.

Per CONTRACT §5:  schedule TTL 15min, groups/teachers TTL 1h.
Per RFC-002 §3.2.5: INSERT ... ON CONFLICT DO UPDATE (upsert), expires_at column.
Per CONTRACT §4:  crm_cache is the source of truth for cached CRM data.

Schema (migrations/001_redis_to_postgres.sql):
    crm_cache (cache_key TEXT PRIMARY KEY, payload JSONB, expires_at TIMESTAMPTZ, ...)
"""

import logging
from functools import lru_cache
from typing import Any

from app.storage.postgres import postgres_storage as db

logger = logging.getLogger(__name__)


class ImpulseCache:
    """PostgreSQL-backed cache for Impulse CRM entities (CONTRACT §5).

    Public API is identical to the Redis version so adapter.py needs no changes:
        cache.get(entity, *key_parts) → list | dict | None
        cache.set(entity, value, *key_parts) → None
        cache.delete(entity, *key_parts) → None
        cache.clear_entity(entity) → None
    """

    # TTL in seconds (CONTRACT §5, RFC §20.3)
    SCHEDULE_TTL = 15 * 60   # 15 minutes
    GROUPS_TTL   = 60 * 60   # 1 hour
    TEACHERS_TTL = 60 * 60   # 1 hour

    def _get_key(self, entity: str, *args: str | int) -> str:
        """Build a cache_key string from entity + optional qualifiers.

        Format mirrors the old Redis key: "impulse:cache:{entity}[:{arg}...]"
        Kept identical so existing key patterns remain valid.
        """
        parts = [f"impulse:cache:{entity}"]
        parts.extend(str(a) for a in args)
        return ":".join(parts)

    def _get_ttl(self, entity: str) -> int:
        """Return TTL in seconds for a given entity type."""
        ttl_map = {
            "schedule":  self.SCHEDULE_TTL,
            "group":     self.GROUPS_TTL,
            "groups":    self.GROUPS_TTL,
            "teacher":   self.TEACHERS_TTL,
            "teachers":  self.TEACHERS_TTL,
        }
        return ttl_map.get(entity, 60 * 60)  # default 1 hour

    # ------------------------------------------------------------------
    # Core cache operations
    # ------------------------------------------------------------------

    async def get(
        self,
        entity: str,
        *key_parts: str | int,
    ) -> list[dict[str, Any]] | dict[str, Any] | None:
        """Return cached payload, or None if missing / expired.

        RFC-002 §3.2.5: SELECT WHERE cache_key = $1 AND expires_at > now()
        The JSONB codec on the pool deserializes payload automatically.
        """
        key = self._get_key(entity, *key_parts)
        row = await db.fetchrow(
            """
            SELECT payload FROM crm_cache
            WHERE cache_key = $1
              AND expires_at > NOW()
            """,
            key,
        )
        return row["payload"] if row else None

    async def set(
        self,
        entity: str,
        value: list[dict[str, Any]] | dict[str, Any],
        *key_parts: str | int,
    ) -> None:
        """Upsert payload into crm_cache with a computed expires_at.

        RFC-002 §3.2.5: INSERT ... ON CONFLICT DO UPDATE.
        make_interval(secs => $3) converts the integer TTL to a PG interval.
        The JSONB codec serializes the dict/list automatically.
        """
        key = self._get_key(entity, *key_parts)
        ttl = self._get_ttl(entity)
        await db.execute(
            """
            INSERT INTO crm_cache (cache_key, payload, expires_at)
            VALUES ($1, $2, NOW() + make_interval(secs => $3))
            ON CONFLICT (cache_key) DO UPDATE SET
                payload    = EXCLUDED.payload,
                expires_at = EXCLUDED.expires_at,
                updated_at = NOW()
            """,
            key, value, ttl,
        )

    async def delete(self, entity: str, *key_parts: str | int) -> None:
        """Hard-delete a single cache entry by exact key."""
        key = self._get_key(entity, *key_parts)
        await cache_invalidate(key)

    async def clear_entity(self, entity: str) -> None:
        """Delete all cache entries whose key starts with the entity prefix.

        Replaces Redis SCAN + bulk DELETE.
        RFC-002: cache_invalidate_pattern uses LIKE prefix match.
        """
        prefix = f"impulse:cache:{entity}"
        deleted = await cache_invalidate_pattern(prefix)
        logger.debug("impulse_cache: cleared entity=%s rows_deleted=%d", entity, deleted)


# ------------------------------------------------------------------
# Low-level helpers (also importable by other modules if needed)
# ------------------------------------------------------------------

async def cache_get(key: str) -> dict | list | None:
    """Return payload for an exact cache_key, or None if missing/expired."""
    row = await db.fetchrow(
        """
        SELECT payload FROM crm_cache
        WHERE cache_key = $1
          AND expires_at > NOW()
        """,
        key,
    )
    return row["payload"] if row else None


async def cache_set(key: str, value: dict | list, ttl_seconds: int) -> None:
    """Upsert a cache entry with an explicit TTL (in seconds)."""
    await db.execute(
        """
        INSERT INTO crm_cache (cache_key, payload, expires_at)
        VALUES ($1, $2, NOW() + make_interval(secs => $3))
        ON CONFLICT (cache_key) DO UPDATE SET
            payload    = EXCLUDED.payload,
            expires_at = EXCLUDED.expires_at,
            updated_at = NOW()
        """,
        key, value, ttl_seconds,
    )


async def cache_invalidate(key: str) -> None:
    """Delete a single cache entry by exact key."""
    await db.execute(
        "DELETE FROM crm_cache WHERE cache_key = $1",
        key,
    )


async def cache_invalidate_pattern(prefix: str) -> int:
    """Delete all cache entries whose cache_key starts with prefix.

    Replaces Redis SCAN + multi-key DELETE.
    Returns the number of rows deleted.
    """
    result = await db.execute(
        "DELETE FROM crm_cache WHERE cache_key LIKE $1 || '%'",
        prefix,
    )
    # asyncpg returns 'DELETE N' — parse the count
    try:
        return int(result.split()[-1])
    except (ValueError, IndexError, AttributeError):
        return 0


@lru_cache()
def get_impulse_cache() -> ImpulseCache:
    """Return the shared ImpulseCache instance (lazy, cached)."""
    return ImpulseCache()
