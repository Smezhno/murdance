"""Outbound message queue — CONTRACT §9.

Every outbound message goes through outbound_queue → worker sends.

Schema (migrations/001_redis_to_postgres.sql):
    outbound_queue: id, channel, chat_id, text, payload JSONB, trace_id UUID,
                    scheduled_at, priority, status outbound_status,
                    attempts, next_attempt_at, last_error, created_at, updated_at

Retry policy (CONTRACT §9): 0s → 5s → 30s → DLQ (dead_letter_messages)
Max retries: 3 (attempts 0, 1, 2 → on 3rd failure → status='failed' becomes 'dlq')

LISTEN/NOTIFY: after every INSERT, NOTIFY outbound_new so the worker wakes
immediately instead of waiting for the next poll cycle (RFC-002 §3.3).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from app.storage.postgres import postgres_storage as db

# Retry delays per attempt number (CONTRACT §9: 0s → 5s → 30s → DLQ)
_RETRY_DELAYS_SECONDS = [0, 5, 30]
MAX_RETRIES = len(_RETRY_DELAYS_SECONDS)  # 3 — on the 4th failure → DLQ


async def enqueue_message(
    chat_id: str,
    channel: str,
    text: str,
    trace_id: UUID,
    priority: int = 0,
    scheduled_at: datetime | None = None,
) -> int:
    """Insert a plain-text message into outbound_queue.

    CONTRACT §9: ALL outgoing messages go through this queue.
    NOTIFY outbound_new wakes the worker immediately (RFC-002 §3.3).

    Args:
        chat_id:      Recipient chat ID (channel-specific).
        channel:      'telegram' | 'whatsapp' | ...
        text:         Message body (worker truncates to channel limit).
        trace_id:     Trace UUID for observability (CONTRACT §17).
        priority:     0 = highest, higher = lower priority (default 0).
        scheduled_at: None → send ASAP. Future datetime → reminder SLA.

    Returns:
        Inserted row ID.
    """
    row = await db.fetchrow(
        """
        INSERT INTO outbound_queue
            (channel, chat_id, text, payload, trace_id, priority, scheduled_at)
        VALUES ($1, $2, $3, $4, $5::uuid, $6, COALESCE($7, NOW()))
        RETURNING id
        """,
        channel,
        chat_id,
        text,
        {"type": "text"},
        str(trace_id),
        priority,
        scheduled_at,
    )
    await db.execute("SELECT pg_notify('outbound_new', $1)", str(row["id"]))
    return row["id"]


async def enqueue_buttons(
    chat_id: str,
    channel: str,
    text: str,
    buttons: list[dict[str, Any]],
    trace_id: UUID,
    priority: int = 0,
) -> int:
    """Insert a message with inline keyboard buttons into outbound_queue.

    Args:
        chat_id:  Recipient chat ID.
        channel:  'telegram' | 'whatsapp' | ...
        text:     Message body shown above the buttons.
        buttons:  List of dicts: [{"text": "...", "callback_data": "..."}]
        trace_id: Trace UUID for observability.
        priority: 0 = highest (default 0).

    Returns:
        Inserted row ID.
    """
    row = await db.fetchrow(
        """
        INSERT INTO outbound_queue
            (channel, chat_id, text, payload, trace_id, priority, scheduled_at)
        VALUES ($1, $2, $3, $4, $5::uuid, $6, NOW())
        RETURNING id
        """,
        channel,
        chat_id,
        text,
        {"type": "buttons", "buttons": buttons},
        str(trace_id),
        priority,
    )
    await db.execute("SELECT pg_notify('outbound_new', $1)", str(row["id"]))
    return row["id"]


async def mark_sent(message_id: int) -> None:
    """Mark a message as successfully delivered.

    Called by the worker after the channel adapter confirms delivery.
    """
    await db.execute(
        """
        UPDATE outbound_queue
           SET status = 'sent', updated_at = NOW()
         WHERE id = $1
        """,
        message_id,
    )


async def mark_failed(message_id: int, error: str) -> None:
    """Record a delivery failure and advance the retry state.

    Retry policy (CONTRACT §9): 0s → 5s → 30s → DLQ.
    - attempts < MAX_RETRIES: set status='pending', schedule next_attempt_at.
    - attempts >= MAX_RETRIES: set status='failed' (worker moves to DLQ).

    The worker checks status='failed' and moves rows to dead_letter_messages
    when DLQ count > 10 (CONTRACT §9).
    """
    row = await db.fetchrow(
        "SELECT attempts FROM outbound_queue WHERE id = $1",
        message_id,
    )
    if row is None:
        return

    attempts_done = int(row["attempts"]) + 1  # this failure counts

    if attempts_done >= MAX_RETRIES:
        # All retries exhausted → mark failed (worker will DLQ it)
        await db.execute(
            """
            UPDATE outbound_queue
               SET status     = 'failed',
                   attempts   = $2,
                   last_error = $3,
                   updated_at = NOW()
             WHERE id = $1
            """,
            message_id,
            attempts_done,
            error[:2000],
        )
    else:
        delay = _RETRY_DELAYS_SECONDS[attempts_done]
        next_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
        await db.execute(
            """
            UPDATE outbound_queue
               SET status          = 'pending',
                   attempts        = $2,
                   next_attempt_at = $3,
                   last_error      = $4,
                   updated_at      = NOW()
             WHERE id = $1
            """,
            message_id,
            attempts_done,
            next_at,
            error[:2000],
        )


async def mark_retry(message_id: int) -> None:
    """Reset a message back to 'pending' so it is eligible for the next poll.

    Used when the worker wants to release a 'sending' lock without counting
    it as a failure (e.g. graceful shutdown mid-send).
    """
    await db.execute(
        """
        UPDATE outbound_queue
           SET status = 'pending', updated_at = NOW()
         WHERE id = $1
        """,
        message_id,
    )


async def get_dlq_count() -> int:
    """Return the number of messages in the dead-letter state.

    CONTRACT §9: DLQ > 10 → alert admin.
    Worker calls this after each batch to decide whether to alert.
    """
    result = await db.fetchval(
        "SELECT COUNT(*) FROM outbound_queue WHERE status = 'failed'"
    )
    return int(result or 0)
