"""Periodic database cleanup — runs every hour inside the app process.

Per RFC-002 §3.1 (cleanup SQL comments) and CONTRACT §9 (DLQ manual review).

Tables cleaned:
  idempotency_locks  — rows older than 24h (audit window per migration comment)
  crm_cache          — expired rows (expires_at < now())
  seen_messages      — expired rows (expires_at < now(); default TTL is 5 min)
  outbound_queue     — status='sent' rows older than 7 days
  budget_counters    — rows older than 7 days (trend analysis window)

NOT cleaned:
  outbound_queue WHERE status='failed' (DLQ) — requires manual review (CONTRACT §9)
  outbound_queue WHERE status='cancelled'    — kept for audit
  Any audit/observability tables (messages, booking_attempts, etc.)
"""

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.storage.postgres import postgres_storage as db

logger = structlog.get_logger(__name__)

# Interval between cleanup runs.
_INTERVAL_HOURS = 1


async def run_cleanup() -> None:
    """Execute all cleanup queries and log deleted row counts."""
    log = logger.bind(task="db_cleanup")

    queries: list[tuple[str, str]] = [
        (
            "idempotency_locks",
            "DELETE FROM idempotency_locks WHERE created_at < NOW() - INTERVAL '24 hours'",
        ),
        (
            "crm_cache",
            "DELETE FROM crm_cache WHERE expires_at < NOW()",
        ),
        (
            "seen_messages",
            "DELETE FROM seen_messages WHERE expires_at < NOW()",
        ),
        (
            "outbound_queue[sent]",
            "DELETE FROM outbound_queue WHERE status = 'sent' AND created_at < NOW() - INTERVAL '7 days'",
        ),
        (
            "budget_counters",
            "DELETE FROM budget_counters WHERE created_at < NOW() - INTERVAL '7 days'",
        ),
    ]

    total = 0
    for table, sql in queries:
        try:
            result: str = await db.execute(sql)
            # asyncpg returns e.g. "DELETE 42" — parse the count.
            deleted = int(result.split()[-1]) if result.startswith("DELETE") else 0
            total += deleted
            if deleted:
                log.info("cleanup.deleted", table=table, rows=deleted)
            else:
                log.debug("cleanup.nothing", table=table)
        except Exception:
            log.exception("cleanup.error", table=table)

    log.info("cleanup.done", total_deleted=total)


def start_scheduler() -> AsyncIOScheduler:
    """Create, configure, and start the APScheduler instance.

    Returns the running scheduler so the caller can shut it down cleanly.
    The first run is scheduled immediately (next_run_time=None triggers on
    the first interval tick; misfire_grace_time lets a delayed run still fire).
    """
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_cleanup,
        trigger="interval",
        hours=_INTERVAL_HOURS,
        id="db_cleanup",
        replace_existing=True,
        misfire_grace_time=300,  # fire up to 5 min late if app was busy
    )
    scheduler.start()
    logger.info("cleanup.scheduler_started", interval_hours=_INTERVAL_HOURS)
    return scheduler
