"""Redis connection pool and utilities.

Per CONTRACT §3: Redis stores sessions, locks, cache, queues, budget counters.
"""

import json
from typing import Any

import redis.asyncio as redis
from redis.asyncio import ConnectionPool, Redis

from app.config import get_settings


class RedisStorage:
    """Redis storage with connection pool."""

    def __init__(self) -> None:
        """Initialize Redis connection pool."""
        self._pool: ConnectionPool | None = None
        self._client: Redis | None = None

    async def connect(self) -> None:
        """Create connection pool and connect to Redis."""
        settings = get_settings()
        self._pool = ConnectionPool.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            max_connections=50,
        )
        self._client = Redis(connection_pool=self._pool)

    async def disconnect(self) -> None:
        """Close connection pool."""
        if self._client:
            await self._client.close()
        if self._pool:
            await self._pool.disconnect()

    @property
    def client(self) -> Redis:
        """Get Redis client. Raises if not connected."""
        if self._client is None:
            raise RuntimeError("Redis not connected. Call connect() first.")
        return self._client

    async def set_json(self, key: str, value: Any, ex: int | None = None) -> bool:
        """Set JSON value with optional expiration."""
        json_str = json.dumps(value, default=str)
        return await self.client.set(key, json_str, ex=ex)

    async def get_json(self, key: str) -> Any | None:
        """Get JSON value."""
        value = await self.client.get(key)
        if value is None:
            return None
        return json.loads(value)

    async def setnx(self, key: str, value: str, ex: int | None = None) -> bool:
        """Set if not exists with optional expiration."""
        return await self.client.set(key, value, nx=True, ex=ex)

    async def delete(self, *keys: str) -> int:
        """Delete keys."""
        return await self.client.delete(*keys)

    async def exists(self, key: str) -> bool:
        """Check if key exists."""
        return await self.client.exists(key) > 0

    async def expire(self, key: str, seconds: int) -> bool:
        """Set expiration on key."""
        return await self.client.expire(key, seconds)

    async def lpush(self, key: str, *values: str) -> int:
        """Push values to list (left)."""
        return await self.client.lpush(key, *values)

    async def rpop(self, key: str, timeout: int = 0) -> str | None:
        """Pop value from list (right)."""
        if timeout > 0:
            result = await self.client.brpop(key, timeout=timeout)
            return result[1] if result else None
        return await self.client.rpop(key)

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        """Add members to sorted set."""
        return await self.client.zadd(key, mapping)

    async def zpopmin(self, key: str, count: int = 1) -> list[tuple[str, float]]:
        """Pop members with lowest scores from sorted set."""
        return await self.client.zpopmin(key, count)

    async def incr(self, key: str, amount: int = 1) -> int:
        """Increment key by amount."""
        return await self.client.incrby(key, amount)

    async def get(self, key: str) -> str | None:
        """Get string value."""
        return await self.client.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        """Set string value with optional expiration."""
        return await self.client.set(key, value, ex=ex)

    async def llen(self, key: str) -> int:
        """Get list length."""
        return await self.client.llen(key)

    async def scan_delete(self, pattern: str, count: int = 100) -> int:
        """Scan and delete keys matching pattern.

        Args:
            pattern: Key pattern (e.g., "impulse:cache:schedule:*")
            count: Number of keys to scan per iteration

        Returns:
            Number of keys deleted
        """
        deleted = 0
        cursor = 0
        while True:
            cursor, keys = await self.client.scan(cursor, match=pattern, count=count)
            if keys:
                deleted += await self.client.delete(*keys)
            if cursor == 0:
                break
        return deleted

    async def health_check(self) -> bool:
        """Check Redis connection health."""
        try:
            await self.client.ping()
            return True
        except Exception:
            return False


# Global Redis storage instance
redis_storage = RedisStorage()
