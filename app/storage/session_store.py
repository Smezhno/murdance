"""PostgreSQL-backed session store.

Replaces Redis GET/SET session:{channel}:{chat_id} with the `sessions` table.

Per CONTRACT §3, §4:  Sessions live in Postgres, source of truth.
Per RFC-002 §3.2.1:   get_session / save_session patterns.
Per CONTRACT §20:      get_sessions_by_state used for startup session recovery.

Schema (from migrations/001_redis_to_postgres.sql):
    sessions (channel, chat_id) PRIMARY KEY
    fsm_state   VARCHAR   — ConversationState.value
    slots       JSONB     — SlotValues as dict
    history     JSONB     — reserved (currently [])
    metadata    JSONB     — {trace_id, ...}
    expires_at  TIMESTAMPTZ
    created_at  TIMESTAMPTZ
    updated_at  TIMESTAMPTZ
"""

import logging
from datetime import datetime, timezone

from app.models import ConversationState, Session, SlotValues
from app.storage.postgres import postgres_storage as db

logger = logging.getLogger(__name__)


def _row_to_session(row: object) -> Session | None:
    """Deserialize an asyncpg Record into a Session. Returns None on any error."""
    try:
        slots_dict: dict = row["slots"] or {}
        metadata: dict = row["metadata"] or {}

        # Reconstruct SlotValues; unknown keys are silently ignored by Pydantic
        slots = SlotValues.model_validate(slots_dict)

        # trace_id is stored in metadata; fall back to a fresh UUID if missing
        from uuid import UUID, uuid4
        raw_trace_id = metadata.get("trace_id")
        trace_id = UUID(raw_trace_id) if raw_trace_id else uuid4()

        return Session(
            trace_id=trace_id,
            channel=row["channel"],
            chat_id=row["chat_id"],
            state=ConversationState(row["fsm_state"]),
            slots=slots,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            expires_at=row["expires_at"],
        )
    except Exception:
        logger.exception(
            "session_store: failed to deserialize session channel=%s chat_id=%s",
            row["channel"],
            row["chat_id"],
        )
        return None


async def get_session(channel: str, chat_id: str) -> Session | None:
    """Load session from Postgres.

    Returns None if:
    - row not found
    - expires_at is in the past (expired)
    - deserialization fails

    RFC-002 §3.2.1: equivalent of Redis GET + TTL check.
    """
    row = await db.fetchrow(
        """
        SELECT channel, chat_id, fsm_state, slots, history, metadata,
               expires_at, created_at, updated_at
        FROM sessions
        WHERE channel = $1 AND chat_id = $2
        """,
        channel, chat_id,
    )
    if row is None:
        return None

    # Treat expired sessions the same as missing (caller will recreate)
    if row["expires_at"] is not None:
        now = datetime.now(timezone.utc)
        if row["expires_at"] < now:
            logger.debug(
                "session_store: session expired channel=%s chat_id=%s", channel, chat_id
            )
            return None

    return _row_to_session(row)


async def save_session(session: Session) -> None:
    """Upsert session into Postgres.

    On INSERT conflict (channel, chat_id) → UPDATE all mutable columns.
    RFC-002 §3.2.1: equivalent of Redis SET with EX.
    CONTRACT §20 migration note: expires_at MUST be set before calling this;
    conversation.py already does so via get_timeout_seconds().
    """
    metadata = {"trace_id": str(session.trace_id)}
    slots_dict = session.slots.model_dump(mode="json")

    await db.execute(
        """
        INSERT INTO sessions
            (channel, chat_id, fsm_state, slots, history, metadata,
             expires_at, created_at, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        ON CONFLICT (channel, chat_id) DO UPDATE SET
            fsm_state  = EXCLUDED.fsm_state,
            slots      = EXCLUDED.slots,
            metadata   = EXCLUDED.metadata,
            expires_at = EXCLUDED.expires_at,
            updated_at = EXCLUDED.updated_at
        """,
        session.channel,
        session.chat_id,
        session.state.value,
        slots_dict,
        [],              # history — reserved, kept in slots.messages for now
        metadata,
        session.expires_at,
        session.created_at,
        session.updated_at,
    )


async def delete_session(channel: str, chat_id: str) -> None:
    """Delete a session row (e.g. after explicit logout or test teardown)."""
    await db.execute(
        "DELETE FROM sessions WHERE channel = $1 AND chat_id = $2",
        channel, chat_id,
    )


async def get_sessions_by_state(
    state: ConversationState,
    older_than_minutes: int,
) -> list[Session]:
    """Return sessions in a given FSM state whose updated_at is older than the threshold.

    Used by session recovery on startup (CONTRACT §20):
        BOOKING_IN_PROGRESS > 1 min  → fallback + notify
        ADMIN_RESPONDING    > 240 min → notify + IDLE
        Any state           > 1440 min → IDLE

    Returns only rows that can be successfully deserialized.
    """
    rows = await db.fetch(
        """
        SELECT channel, chat_id, fsm_state, slots, history, metadata,
               expires_at, created_at, updated_at
        FROM sessions
        WHERE fsm_state = $1
          AND updated_at < NOW() - ($2 || ' minutes')::INTERVAL
        """,
        state.value,
        str(older_than_minutes),
    )
    sessions = []
    for row in rows:
        session = _row_to_session(row)
        if session is not None:
            sessions.append(session)
    return sessions
