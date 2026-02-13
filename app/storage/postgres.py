"""PostgreSQL async connection and table creation.

Per CONTRACT §3: Postgres stores message logs, audit, booking attempts, errors, LLM cost.
Per CONTRACT §17: Tables: messages, booking_attempts, tool_calls, llm_calls, errors, dead_letter_messages.
"""

import json
from datetime import datetime
from uuid import UUID

import asyncpg
from asyncpg import Connection, Pool

from app.config import get_settings


class PostgresStorage:
    """PostgreSQL storage with async connection pool."""

    def __init__(self) -> None:
        """Initialize PostgreSQL storage."""
        self._pool: Pool | None = None

    async def connect(self) -> None:
        """Create connection pool and connect to PostgreSQL."""
        settings = get_settings()
        self._pool = await asyncpg.create_pool(
            settings.postgres_url,
            min_size=2,
            max_size=10,
        )

    async def disconnect(self) -> None:
        """Close connection pool."""
        if self._pool:
            await self._pool.close()

    async def acquire(self) -> Connection:
        """Acquire connection from pool."""
        if self._pool is None:
            raise RuntimeError("Postgres not connected. Call connect() first.")
        return await self._pool.acquire()

    def release(self, conn: Connection) -> None:
        """Release connection to pool."""
        if self._pool:
            self._pool.release(conn)

    async def execute(self, query: str, *args: object) -> str:
        """Execute query and return result."""
        async with self._pool.acquire() as conn:
            return await conn.execute(query, *args)

    async def fetch(self, query: str, *args: object) -> list[asyncpg.Record]:
        """Fetch rows."""
        async with self._pool.acquire() as conn:
            return await conn.fetch(query, *args)

    async def fetchrow(self, query: str, *args: object) -> asyncpg.Record | None:
        """Fetch single row."""
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(query, *args)

    async def create_tables(self) -> None:
        """Create all Postgres tables per CONTRACT §17."""
        async with self._pool.acquire() as conn:
            # Messages table (CONTRACT §17)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id BIGSERIAL PRIMARY KEY,
                    trace_id UUID NOT NULL,
                    channel VARCHAR(20) NOT NULL,
                    chat_id VARCHAR(255) NOT NULL,
                    message_id VARCHAR(255) NOT NULL,
                    timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
                    text TEXT NOT NULL,
                    message_type VARCHAR(20) NOT NULL,
                    sender_phone VARCHAR(50),
                    sender_name VARCHAR(255),
                    direction VARCHAR(10) NOT NULL CHECK (direction IN ('inbound', 'outbound')),
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                    UNIQUE(channel, message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_messages_trace_id ON messages(trace_id);
                CREATE INDEX IF NOT EXISTS idx_messages_channel_chat_id ON messages(channel, chat_id);
                CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
            """)

            # Booking attempts table (CONTRACT §17)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS booking_attempts (
                    id BIGSERIAL PRIMARY KEY,
                    trace_id UUID NOT NULL,
                    channel VARCHAR(20) NOT NULL,
                    chat_id VARCHAR(255) NOT NULL,
                    group_id VARCHAR(255),
                    schedule_id VARCHAR(255),
                    datetime TIMESTAMP WITH TIME ZONE,
                    client_name VARCHAR(255),
                    client_phone VARCHAR(50),
                    success BOOLEAN NOT NULL,
                    error_message TEXT,
                    crm_response JSONB,
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_booking_attempts_trace_id ON booking_attempts(trace_id);
                CREATE INDEX IF NOT EXISTS idx_booking_attempts_channel_chat_id ON booking_attempts(channel, chat_id);
                CREATE INDEX IF NOT EXISTS idx_booking_attempts_success ON booking_attempts(success);
            """)

            # Tool calls table (CONTRACT §17)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS tool_calls (
                    id BIGSERIAL PRIMARY KEY,
                    trace_id UUID NOT NULL,
                    tool_name VARCHAR(100) NOT NULL,
                    parameters JSONB NOT NULL,
                    result JSONB,
                    error TEXT,
                    duration_ms INTEGER,
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_tool_calls_trace_id ON tool_calls(trace_id);
                CREATE INDEX IF NOT EXISTS idx_tool_calls_tool_name ON tool_calls(tool_name);
                CREATE INDEX IF NOT EXISTS idx_tool_calls_created_at ON tool_calls(created_at);
            """)

            # LLM calls table (CONTRACT §17)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS llm_calls (
                    id BIGSERIAL PRIMARY KEY,
                    trace_id UUID NOT NULL,
                    provider VARCHAR(50) NOT NULL,
                    model VARCHAR(100) NOT NULL,
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER,
                    total_tokens INTEGER,
                    cost_usd DECIMAL(10, 6),
                    request_json JSONB,
                    response_json JSONB,
                    error TEXT,
                    duration_ms INTEGER,
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_llm_calls_trace_id ON llm_calls(trace_id);
                CREATE INDEX IF NOT EXISTS idx_llm_calls_provider ON llm_calls(provider);
                CREATE INDEX IF NOT EXISTS idx_llm_calls_created_at ON llm_calls(created_at);
            """)

            # Errors table (CONTRACT §17)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS errors (
                    id BIGSERIAL PRIMARY KEY,
                    trace_id UUID,
                    error_type VARCHAR(100) NOT NULL,
                    error_message TEXT NOT NULL,
                    stack_trace TEXT,
                    context JSONB,
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_errors_trace_id ON errors(trace_id);
                CREATE INDEX IF NOT EXISTS idx_errors_error_type ON errors(error_type);
                CREATE INDEX IF NOT EXISTS idx_errors_created_at ON errors(created_at);
            """)

            # Dead letter messages table (CONTRACT §17, §9)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS dead_letter_messages (
                    id BIGSERIAL PRIMARY KEY,
                    trace_id UUID,
                    channel VARCHAR(20) NOT NULL,
                    chat_id VARCHAR(255) NOT NULL,
                    text TEXT NOT NULL,
                    error TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 1,
                    last_attempt_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_dlq_trace_id ON dead_letter_messages(trace_id);
                CREATE INDEX IF NOT EXISTS idx_dlq_channel ON dead_letter_messages(channel);
                CREATE INDEX IF NOT EXISTS idx_dlq_created_at ON dead_letter_messages(created_at);
            """)

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
        """Log message to Postgres (CONTRACT §17)."""
        await self.execute("""
            INSERT INTO messages (
                trace_id, channel, chat_id, message_id, timestamp, text,
                message_type, sender_phone, sender_name, direction
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            ON CONFLICT (channel, message_id) DO NOTHING
        """, trace_id, channel, chat_id, message_id, timestamp, text,
            message_type, sender_phone, sender_name, direction)

    async def log_booking_attempt(
        self,
        trace_id: UUID,
        channel: str,
        chat_id: str,
        success: bool,
        group_id: str | None = None,
        schedule_id: str | None = None,
        datetime: datetime | None = None,
        client_name: str | None = None,
        client_phone: str | None = None,
        error_message: str | None = None,
        crm_response: dict | None = None,
    ) -> None:
        """Log booking attempt to Postgres (CONTRACT §17)."""
        crm_response_json = json.dumps(crm_response) if crm_response else None
        await self.execute("""
            INSERT INTO booking_attempts (
                trace_id, channel, chat_id, group_id, schedule_id, datetime,
                client_name, client_phone, success, error_message, crm_response
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        """, trace_id, channel, chat_id, group_id, schedule_id, datetime,
            client_name, client_phone, success, error_message, crm_response_json)

    async def log_tool_call(
        self,
        trace_id: UUID,
        tool_name: str,
        parameters: dict,
        result: dict | None = None,
        error: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """Log tool call to Postgres (CONTRACT §17)."""
        parameters_json = json.dumps(parameters)
        result_json = json.dumps(result) if result else None
        await self.execute("""
            INSERT INTO tool_calls (
                trace_id, tool_name, parameters, result, error, duration_ms
            ) VALUES ($1, $2, $3, $4, $5, $6)
        """, trace_id, tool_name, parameters_json, result_json, error, duration_ms)

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
        """Log LLM call to Postgres (CONTRACT §17)."""
        request_json_str = json.dumps(request_json) if request_json else None
        response_json_str = json.dumps(response_json) if response_json else None
        await self.execute("""
            INSERT INTO llm_calls (
                trace_id, provider, model, prompt_tokens, completion_tokens,
                total_tokens, cost_usd, request_json, response_json, error, duration_ms
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        """, trace_id, provider, model, prompt_tokens, completion_tokens,
            total_tokens, cost_usd, request_json_str, response_json_str, error, duration_ms)

    async def log_error(
        self,
        error_type: str,
        error_message: str,
        trace_id: UUID | None = None,
        stack_trace: str | None = None,
        context: dict | None = None,
    ) -> None:
        """Log error to Postgres (CONTRACT §17)."""
        context_json = json.dumps(context) if context else None
        await self.execute("""
            INSERT INTO errors (
                trace_id, error_type, error_message, stack_trace, context
            ) VALUES ($1, $2, $3, $4, $5)
        """, trace_id, error_type, error_message, stack_trace, context_json)

    async def log_dead_letter(
        self,
        channel: str,
        chat_id: str,
        text: str,
        error: str,
        attempts: int = 1,
        trace_id: UUID | None = None,
    ) -> None:
        """Log dead letter message to Postgres (CONTRACT §17, §9)."""
        await self.execute("""
            INSERT INTO dead_letter_messages (
                trace_id, channel, chat_id, text, error, attempts
            ) VALUES ($1, $2, $3, $4, $5, $6)
        """, trace_id, channel, chat_id, text, error, attempts)

    async def health_check(self) -> bool:
        """Check PostgreSQL connection health."""
        try:
            async with self._pool.acquire() as conn:
                await conn.execute("SELECT 1")
            return True
        except Exception:
            return False


# Global Postgres storage instance
postgres_storage = PostgresStorage()
