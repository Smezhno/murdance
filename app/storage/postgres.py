"""PostgreSQL async connection pool, generic query helpers, and audit logging.

Per CONTRACT §3:  Postgres stores all data — sessions, cache, queues, budgets, logs.
Per CONTRACT §17: Audit tables created by migrations/000_audit_tables.sql.
Per RFC-002 §5.1: Migrations run automatically on startup before serving traffic.
"""

import json
import logging
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg
from asyncpg import Pool

from app.config import get_settings
from app.storage.pg_helpers import pool_stats, run_migrations

logger = logging.getLogger(__name__)


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Register JSONB codec so asyncpg serializes/deserializes dicts automatically.

    Without this, passing a Python dict to a JSONB column either raises
    TypeError or double-serializes (str inside JSONB). With this codec,
    callers pass raw dicts/lists — no json.dumps() needed at call sites.
    """
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )


class PostgresStorage:
    """Async PostgreSQL storage: connection pool + query helpers + audit logging."""

    def __init__(self) -> None:
        self._pool: Pool | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Create pool (min=2, max=10), register JSONB codec, run migrations.

        Called once from FastAPI lifespan on startup. All DDL (including audit
        tables) is applied by run_migrations() from the migrations/ directory.
        RFC-002 §8: pool metrics available via pool_stats() for /health.
        """
        settings = get_settings()
        self._pool = await asyncpg.create_pool(
            settings.postgres_url,
            min_size=2,
            max_size=10,
            init=_init_connection,
        )
        logger.info("postgres: pool created (min=2, max=10)")
        await run_migrations(self._pool)
        logger.info("postgres: ready")

    async def disconnect(self) -> None:
        """Gracefully close pool on shutdown."""
        if self._pool:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> Pool:
        if self._pool is None:
            raise RuntimeError("Postgres not connected. Call connect() first.")
        return self._pool

    # ------------------------------------------------------------------
    # Generic query helpers  (RFC-002 §3.2 patterns)
    # ------------------------------------------------------------------

    async def execute(self, query: str, *args: object) -> str:
        """Execute a DML statement; return command status string (e.g. 'UPDATE 3')."""
        async with self.pool.acquire() as conn:
            return await conn.execute(query, *args)

    async def fetch(self, query: str, *args: object) -> list[asyncpg.Record]:
        """Return all matching rows."""
        async with self.pool.acquire() as conn:
            return await conn.fetch(query, *args)

    async def fetchrow(self, query: str, *args: object) -> asyncpg.Record | None:
        """Return the first matching row, or None."""
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(query, *args)

    async def fetchval(self, query: str, *args: object, column: int = 0) -> Any:
        """Return a single scalar value from the first row, or None."""
        async with self.pool.acquire() as conn:
            return await conn.fetchval(query, *args, column=column)

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        """Return True if the pool can reach Postgres."""
        try:
            await self.fetchval("SELECT 1")
            return True
        except Exception:
            return False

    def pool_stats(self) -> dict:
        """Connection pool metrics for the /health endpoint.

        Keys: pool_min_size, pool_max_size, pool_size, pool_free, pool_used.
        RFC-002 §8: alert if pool_used approaches pool_max_size.
        """
        return pool_stats(self.pool)

    # ------------------------------------------------------------------
    # Audit logging (CONTRACT §17)
    # All methods are fire-and-forget: exceptions are logged but never
    # re-raised so a logging failure cannot crash the request handler.
    # JSONB columns receive raw dicts — the pool codec serializes them.
    # ------------------------------------------------------------------

    async def log_message(
        self,
        trace_id: UUID,
        channel: str,
        chat_id: str,
        message_id: str,
        timestamp: datetime,
        text: str,
        message_type: str,
        direction: str,
        sender_phone: str | None = None,
        sender_name: str | None = None,
    ) -> None:
        """Log inbound or outbound message (CONTRACT §17)."""
        try:
            await self.execute(
                """
                INSERT INTO messages (
                    trace_id, channel, chat_id, message_id, timestamp, text,
                    message_type, sender_phone, sender_name, direction
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                ON CONFLICT (channel, message_id) DO NOTHING
                """,
                trace_id, channel, chat_id, message_id, timestamp, text,
                message_type, sender_phone, sender_name, direction,
            )
        except Exception:
            logger.exception("failed to log message trace_id=%s", trace_id)

    async def log_booking_attempt(
        self,
        trace_id: UUID,
        channel: str,
        chat_id: str,
        success: bool,
        group_id: str | None = None,
        schedule_id: str | None = None,
        datetime_: datetime | None = None,
        client_name: str | None = None,
        client_phone: str | None = None,
        error_message: str | None = None,
        crm_response: dict | None = None,
    ) -> None:
        """Log CRM booking attempt (CONTRACT §17)."""
        try:
            await self.execute(
                """
                INSERT INTO booking_attempts (
                    trace_id, channel, chat_id, group_id, schedule_id, datetime,
                    client_name, client_phone, success, error_message, crm_response
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                """,
                trace_id, channel, chat_id, group_id, schedule_id, datetime_,
                client_name, client_phone, success, error_message, crm_response,
            )
        except Exception:
            logger.exception("failed to log booking_attempt trace_id=%s", trace_id)

    async def log_tool_call(
        self,
        trace_id: UUID,
        tool_name: str,
        parameters: dict,
        result: dict | None = None,
        error: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """Log LLM tool call (CONTRACT §17)."""
        try:
            await self.execute(
                """
                INSERT INTO tool_calls (
                    trace_id, tool_name, parameters, result, error, duration_ms
                ) VALUES ($1, $2, $3, $4, $5, $6)
                """,
                trace_id, tool_name, parameters, result, error, duration_ms,
            )
        except Exception:
            logger.exception("failed to log tool_call trace_id=%s", trace_id)

    async def log_llm_call(
        self,
        trace_id: UUID,
        provider: str,
        model: str,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        total_tokens: int | None = None,
        cost_usd: float | None = None,
        request_json: dict | None = None,
        response_json: dict | None = None,
        error: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """Log LLM API call with token/cost metrics (CONTRACT §17)."""
        try:
            await self.execute(
                """
                INSERT INTO llm_calls (
                    trace_id, provider, model, prompt_tokens, completion_tokens,
                    total_tokens, cost_usd, request_json, response_json,
                    error, duration_ms
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                """,
                trace_id, provider, model, prompt_tokens, completion_tokens,
                total_tokens, cost_usd, request_json, response_json,
                error, duration_ms,
            )
        except Exception:
            logger.exception("failed to log llm_call trace_id=%s", trace_id)

    async def log_error(
        self,
        error_type: str,
        error_message: str,
        trace_id: UUID | None = None,
        stack_trace: str | None = None,
        context: dict | None = None,
    ) -> None:
        """Log application error (CONTRACT §17)."""
        try:
            await self.execute(
                """
                INSERT INTO errors (
                    trace_id, error_type, error_message, stack_trace, context
                ) VALUES ($1, $2, $3, $4, $5)
                """,
                trace_id, error_type, error_message, stack_trace, context,
            )
        except Exception:
            logger.exception("failed to log error type=%s", error_type)

    async def log_dead_letter(
        self,
        channel: str,
        chat_id: str,
        text: str,
        error: str,
        attempts: int = 1,
        trace_id: UUID | None = None,
    ) -> None:
        """Log undeliverable outbound message to DLQ (CONTRACT §17, §9)."""
        try:
            await self.execute(
                """
                INSERT INTO dead_letter_messages (
                    trace_id, channel, chat_id, text, error, attempts
                ) VALUES ($1, $2, $3, $4, $5, $6)
                """,
                trace_id, channel, chat_id, text, error, attempts,
            )
        except Exception:
            logger.exception(
                "failed to log dead_letter channel=%s chat_id=%s", channel, chat_id
            )


# Global Postgres storage instance
postgres_storage = PostgresStorage()
