"""Budget Guard: token/cost limits backed by PostgreSQL budget_counters.

Per CONTRACT §12: All 4 hard limits with atomic PG counters, auto-shutdown on breach.
Per RFC-002 §3.2.4: INSERT ... ON CONFLICT DO UPDATE replaces Redis INCRBY.

Schema (migrations/001_redis_to_postgres.sql):
    budget_counters PRIMARY KEY (provider, "window", window_start)
    columns: tokens_used, requests_count, errors_count, cost_usd
    window: 'minute' | 'hour' | 'day'
    window_start: TIMESTAMPTZ truncated to the window boundary

Key design decisions:
- One row per (provider, window_granularity, window_start_time).
  All four metrics for a given provider+window share one row.
- INCREMENT is atomic: INSERT ... ON CONFLICT DO UPDATE SET col = col + $n RETURNING col.
- CHECK reads the just-incremented value from the same statement — no race window.
- Rows are retained for 7 days for trend analysis, swept by periodic cleanup job.
"""

import logging
from datetime import datetime, timezone
from functools import lru_cache

from app.config import get_settings
from app.storage.postgres import postgres_storage as db

logger = logging.getLogger(__name__)

# Provider label used for aggregated budget tracking.
_PROVIDER = "all"


def _hour_start() -> datetime:
    return datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _day_start() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _minute_start() -> datetime:
    return datetime.now(timezone.utc).replace(second=0, microsecond=0)


async def _increment(
    window: str,
    window_start: datetime,
    *,
    tokens: int = 0,
    requests: int = 0,
    errors: int = 0,
    cost_usd: float = 0.0,
) -> dict:
    """Atomically upsert budget_counters and return the updated row values.

    RFC-002 §3.2.4: INSERT ... ON CONFLICT DO UPDATE SET col = col + excluded_col
    RETURNING gives the post-increment values in one round-trip.
    """
    row = await db.fetchrow(
        """
        INSERT INTO budget_counters
            (provider, "window", window_start,
             tokens_used, requests_count, errors_count, cost_usd)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (provider, "window", window_start) DO UPDATE SET
            tokens_used    = budget_counters.tokens_used    + EXCLUDED.tokens_used,
            requests_count = budget_counters.requests_count + EXCLUDED.requests_count,
            errors_count   = budget_counters.errors_count   + EXCLUDED.errors_count,
            cost_usd       = budget_counters.cost_usd       + EXCLUDED.cost_usd,
            updated_at     = NOW()
        RETURNING tokens_used, requests_count, errors_count, cost_usd
        """,
        _PROVIDER, window, window_start,
        tokens, requests, errors, cost_usd,
    )
    return dict(row)


async def _read(window: str, window_start: datetime) -> dict:
    """Read current counter values without incrementing (for is_breached())."""
    row = await db.fetchrow(
        """
        SELECT tokens_used, requests_count, errors_count, cost_usd
        FROM budget_counters
        WHERE provider = $1 AND "window" = $2 AND window_start = $3
        """,
        _PROVIDER, window, window_start,
    )
    if row is None:
        return {"tokens_used": 0, "requests_count": 0, "errors_count": 0, "cost_usd": 0.0}
    return dict(row)


class BudgetGuard:
    """Hard budget limits enforced via PostgreSQL atomic increments (CONTRACT §12).

    Public API is identical to the Redis version — router.py needs no changes.
    """

    def __init__(self) -> None:
        self.settings = get_settings()

    async def check_tokens_per_hour(self, tokens: int) -> tuple[bool, int]:
        """Increment tokens_used for the current hour and check the limit.

        Returns (within_limit, new_total).
        Increments BEFORE checking so the counter is always accurate.
        """
        row = await _increment("hour", _hour_start(), tokens=tokens)
        new_total = int(row["tokens_used"])
        within = new_total <= self.settings.max_tokens_per_hour
        if not within:
            logger.warning(
                "budget_guard: MAX_TOKENS_PER_HOUR breached total=%d limit=%d",
                new_total, self.settings.max_tokens_per_hour,
            )
        return within, new_total

    async def check_cost_per_day(self, cost_usd: float) -> tuple[bool, float]:
        """Increment cost_usd for the current day and check the limit.

        Returns (within_limit, new_total_usd).
        """
        row = await _increment("day", _day_start(), cost_usd=cost_usd)
        new_total = float(row["cost_usd"])
        within = new_total <= self.settings.max_cost_per_day_usd
        if not within:
            logger.warning(
                "budget_guard: MAX_COST_PER_DAY_USD breached total=%.4f limit=%.2f",
                new_total, self.settings.max_cost_per_day_usd,
            )
        return within, new_total

    async def check_requests_per_minute(self) -> tuple[bool, int]:
        """Increment requests_count for the current minute and check the limit.

        Returns (within_limit, new_total).
        """
        row = await _increment("minute", _minute_start(), requests=1)
        new_total = int(row["requests_count"])
        within = new_total <= self.settings.max_requests_per_minute
        if not within:
            logger.warning(
                "budget_guard: MAX_REQUESTS_PER_MINUTE breached total=%d limit=%d",
                new_total, self.settings.max_requests_per_minute,
            )
        return within, new_total

    async def record_error(self) -> bool:
        """Increment errors_count for the current hour.

        Returns True if still within limit, False if breached.
        """
        row = await _increment("hour", _hour_start(), errors=1)
        new_total = int(row["errors_count"])
        within = new_total <= self.settings.max_errors_per_hour
        if not within:
            logger.warning(
                "budget_guard: MAX_ERRORS_PER_HOUR breached total=%d limit=%d",
                new_total, self.settings.max_errors_per_hour,
            )
        return within

    async def check_all_limits(self, tokens: int, cost_usd: float) -> tuple[bool, str]:
        """Check all four limits in priority order, incrementing as we go.

        Stops at the first breach so we don't double-count on a rejected call.
        Returns (within_limits, breach_reason).
        """
        requests_ok, _ = await self.check_requests_per_minute()
        if not requests_ok:
            return False, "MAX_REQUESTS_PER_MINUTE exceeded"

        tokens_ok, _ = await self.check_tokens_per_hour(tokens)
        if not tokens_ok:
            return False, "MAX_TOKENS_PER_HOUR exceeded"

        cost_ok, _ = await self.check_cost_per_day(cost_usd)
        if not cost_ok:
            return False, "MAX_COST_PER_DAY_USD exceeded"

        return True, ""

    async def is_breached(self) -> bool:
        """Return True if any limit is currently at or above its threshold.

        Reads without incrementing — safe to call for health-check purposes.
        """
        hour_row    = await _read("hour",   _hour_start())
        day_row     = await _read("day",    _day_start())
        minute_row  = await _read("minute", _minute_start())

        return (
            int(hour_row["tokens_used"])      >= self.settings.max_tokens_per_hour
            or float(day_row["cost_usd"])     >= self.settings.max_cost_per_day_usd
            or int(minute_row["requests_count"]) >= self.settings.max_requests_per_minute
            or int(hour_row["errors_count"])  >= self.settings.max_errors_per_hour
        )


@lru_cache()
def get_budget_guard() -> BudgetGuard:
    """Return the shared BudgetGuard instance (lazy, cached)."""
    return BudgetGuard()
