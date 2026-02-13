"""Budget Guard: token/cost limits with Redis counters.

Per CONTRACT §12: All 4 limits with Redis counters, auto-shutdown on breach.
"""

from functools import lru_cache
from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.storage.redis import redis_storage


class BudgetGuard:
    """Budget guard with Redis counters (CONTRACT §12)."""

    def __init__(self) -> None:
        """Initialize budget guard with limits from config."""
        self.settings = get_settings()

    def _get_hour_key(self) -> str:
        """Get Redis key for current hour."""
        hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        return f"budget:tokens:hour:{hour.isoformat()}"

    def _get_day_key(self) -> str:
        """Get Redis key for current day."""
        day = datetime.now(timezone.utc).date()
        return f"budget:cost:day:{day.isoformat()}"

    def _get_minute_key(self) -> str:
        """Get Redis key for current minute."""
        minute = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        return f"budget:requests:minute:{minute.isoformat()}"

    def _get_errors_hour_key(self) -> str:
        """Get Redis key for current hour errors."""
        hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        return f"budget:errors:hour:{hour.isoformat()}"

    async def check_tokens_per_hour(self, tokens: int) -> tuple[bool, int]:
        """Check if tokens per hour limit is not exceeded (CONTRACT §12).

        Args:
            tokens: Tokens to add

        Returns:
            Tuple of (within_limit, current_count)
        """
        key = self._get_hour_key()
        # Read current count first
        current_str = await redis_storage.get(key)
        current = int(current_str) if current_str else 0
        
        # Check limit before incrementing
        if current + tokens > self.settings.max_tokens_per_hour:
            await redis_storage.expire(key, 3600)  # Ensure TTL is set
            return False, current
        
        # Increment only if within limit
        new_count = await redis_storage.incr(key, tokens)
        await redis_storage.expire(key, 3600)  # 1 hour TTL

        return True, new_count

    async def check_cost_per_day(self, cost_usd: float) -> tuple[bool, float]:
        """Check if cost per day limit is not exceeded (CONTRACT §12).

        Args:
            cost_usd: Cost in USD to add

        Returns:
            Tuple of (within_limit, current_cost_usd)
        """
        key = self._get_day_key()
        # Store cost as integer (cents) for precision
        cost_cents = int(cost_usd * 100)
        
        # Read current count first
        current_cents_str = await redis_storage.get(key)
        current_cents = int(current_cents_str) if current_cents_str else 0
        current_usd = current_cents / 100.0
        
        # Check limit before incrementing
        if current_usd + cost_usd > self.settings.max_cost_per_day_usd:
            await redis_storage.expire(key, 86400)  # Ensure TTL is set
            return False, current_usd
        
        # Increment only if within limit
        new_cents = await redis_storage.incr(key, cost_cents)
        await redis_storage.expire(key, 86400)  # 24 hours TTL

        new_usd = new_cents / 100.0
        return True, new_usd

    async def check_requests_per_minute(self) -> tuple[bool, int]:
        """Check if requests per minute limit is not exceeded (CONTRACT §12).

        Returns:
            Tuple of (within_limit, current_count)
        """
        key = self._get_minute_key()
        # Read current count first
        current_str = await redis_storage.get(key)
        current = int(current_str) if current_str else 0
        
        # Check limit before incrementing
        if current + 1 > self.settings.max_requests_per_minute:
            await redis_storage.expire(key, 60)  # Ensure TTL is set
            return False, current
        
        # Increment only if within limit
        new_count = await redis_storage.incr(key, 1)
        await redis_storage.expire(key, 60)  # 1 minute TTL

        return True, new_count

    async def record_error(self) -> bool:
        """Record an error and check if errors per hour limit is exceeded (CONTRACT §12).

        Returns:
            True if within limit, False if exceeded
        """
        key = self._get_errors_hour_key()
        current = await redis_storage.incr(key, 1)
        await redis_storage.expire(key, 3600)  # 1 hour TTL

        return current <= self.settings.max_errors_per_hour

    async def check_all_limits(self, tokens: int, cost_usd: float) -> tuple[bool, str]:
        """Check all budget limits (CONTRACT §12).

        Checks limits FIRST, then increments only if within limits.

        Args:
            tokens: Tokens to check
            cost_usd: Cost in USD to check

        Returns:
            Tuple of (within_limits, breach_reason)
            breach_reason is empty string if within limits
        """
        # Check requests per minute (reads current, checks, then increments if OK)
        requests_ok, _ = await self.check_requests_per_minute()
        if not requests_ok:
            return False, "MAX_REQUESTS_PER_MINUTE exceeded"

        # Check tokens per hour (reads current, checks, then increments if OK)
        tokens_ok, _ = await self.check_tokens_per_hour(tokens)
        if not tokens_ok:
            return False, "MAX_TOKENS_PER_HOUR exceeded"

        # Check cost per day (reads current, checks, then increments if OK)
        cost_ok, _ = await self.check_cost_per_day(cost_usd)
        if not cost_ok:
            return False, "MAX_COST_PER_DAY_USD exceeded"

        return True, ""

    async def is_breached(self) -> bool:
        """Check if any budget limit is currently breached.

        Returns:
            True if any limit is breached, False otherwise
        """
        # Check current counters
        tokens_key = self._get_hour_key()
        cost_key = self._get_day_key()
        requests_key = self._get_minute_key()
        errors_key = self._get_errors_hour_key()

        tokens_count = int(await redis_storage.get(tokens_key) or "0")
        cost_cents = int(await redis_storage.get(cost_key) or "0")
        requests_count = int(await redis_storage.get(requests_key) or "0")
        errors_count = int(await redis_storage.get(errors_key) or "0")

        cost_usd = cost_cents / 100.0

        return (
            tokens_count >= self.settings.max_tokens_per_hour
            or cost_usd >= self.settings.max_cost_per_day_usd
            or requests_count >= self.settings.max_requests_per_minute
            or errors_count >= self.settings.max_errors_per_hour
        )


_budget_guard: BudgetGuard | None = None


@lru_cache()
def get_budget_guard() -> BudgetGuard:
    """Get budget guard instance (lazy init)."""
    global _budget_guard
    if _budget_guard is None:
        _budget_guard = BudgetGuard()
    return _budget_guard
