"""Inbound message deduplication.

Per CONTRACT §8:  INSERT into seen_messages (channel, message_id) UNIQUE.
                  UniqueViolationError → duplicate, drop silently.
Per CONTRACT §19: Replay protection — expires_at default 5 min (set in migration DDL).
Per RFC-002:      Replaces Redis SETNX seen:{channel}:{message_id} TTL 5min.
"""

import asyncpg

from app.models import UnifiedMessage
from app.storage.postgres import postgres_storage as db


async def is_duplicate(message: UnifiedMessage) -> bool:
    """Return True if this message has already been processed (CONTRACT §8).

    Attempts an INSERT into seen_messages. The UNIQUE PRIMARY KEY on
    (channel, message_id) makes this atomic — no race window between
    check and write, unlike the old Redis SETNX approach.

    Rows expire naturally via the expires_at column (default: +5 minutes,
    set in migrations/001_redis_to_postgres.sql). Cleanup is done by the
    periodic job; the TTL is not enforced here.
    """
    try:
        await db.execute(
            """
            INSERT INTO seen_messages (channel, message_id, chat_id)
            VALUES ($1, $2, $3)
            """,
            message.channel,
            message.message_id,
            message.chat_id,
        )
        return False  # new message
    except asyncpg.UniqueViolationError:
        return True   # duplicate
