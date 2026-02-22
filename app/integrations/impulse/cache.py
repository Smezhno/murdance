"""Redis cache for Impulse CRM data.

Per CONTRACT §5: schedule 15min, groups 1h, teachers 1h.
"""

import json
from functools import lru_cache
from typing import Any

from app.storage.redis import redis_storage


class ImpulseCache:
    """Redis cache for Impulse CRM entities (CONTRACT §5)."""

    # TTL in seconds
    SCHEDULE_TTL = 15 * 60  # 15 minutes
    GROUPS_TTL = 60 * 60  # 1 hour
    TEACHERS_TTL = 60 * 60  # 1 hour

    def _get_key(self, entity: str, *args: str | int) -> str:
        """Get Redis cache key.

        Args:
            entity: Entity name
            *args: Additional key parts

        Returns:
            Redis key string
        """
        parts = [f"impulse:cache:{entity}"]
        parts.extend(str(arg) for arg in args)
        return ":".join(parts)

    def _get_ttl(self, entity: str) -> int:
        """Get TTL for entity.

        Args:
            entity: Entity name

        Returns:
            TTL in seconds
        """
        ttl_map = {
            "schedule": self.SCHEDULE_TTL,
            "group": self.GROUPS_TTL,
            "groups": self.GROUPS_TTL,
            "teacher": self.TEACHERS_TTL,
            "teachers": self.TEACHERS_TTL,
        }
        return ttl_map.get(entity, 60 * 60)  # Default 1 hour

    async def get(self, entity: str, *key_parts: str | int) -> list[dict[str, Any]] | dict[str, Any] | None:
        """Get cached data.

        Args:
            entity: Entity name
            *key_parts: Additional key parts

        Returns:
            Cached data or None
        """
        key = self._get_key(entity, *key_parts)
        data = await redis_storage.get_json(key)
        return data

    async def set(
        self,
        entity: str,
        value: list[dict[str, Any]] | dict[str, Any],
        *key_parts: str | int,
    ) -> None:
        """Set cached data.

        Args:
            entity: Entity name
            value: Data to cache
            *key_parts: Additional key parts
        """
        key = self._get_key(entity, *key_parts)
        ttl = self._get_ttl(entity)
        await redis_storage.set_json(key, value, ex=ttl)

    async def delete(self, entity: str, *key_parts: str | int) -> None:
        """Delete cached data.

        Args:
            entity: Entity name
            *key_parts: Additional key parts
        """
        key = self._get_key(entity, *key_parts)
        await redis_storage.delete(key)

    async def clear_entity(self, entity: str) -> None:
        """Clear all cache for entity (pattern delete + base key).

        Args:
            entity: Entity name
        """
        # Delete keys with suffix (e.g., impulse:cache:schedule:2026-02-13_None_None)
        pattern = f"impulse:cache:{entity}:*"
        await redis_storage.scan_delete(pattern)
        # Also delete the base key without suffix (e.g., impulse:cache:groups)
        base_key = f"impulse:cache:{entity}"
        await redis_storage.delete(base_key)


@lru_cache()
def get_impulse_cache() -> ImpulseCache:
    """Get Impulse cache instance (lazy init)."""
    return ImpulseCache()

