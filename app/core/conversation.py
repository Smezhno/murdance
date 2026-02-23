"""Conversation session management.

Per CONTRACT §4, §7: Session load/save from PostgreSQL, state transitions, timeout rules.
Per RFC-002 §3.2.1:   Redis GET/SET replaced by session_store.get_session/save_session.
"""

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from app.config import get_settings
from app.core.fsm import can_transition, get_timeout_seconds
from app.models import ConversationState, Session, SlotValues
from app.storage.session_store import (
    delete_session,
    get_session,
    get_sessions_by_state,
    save_session,
)


async def load_session(channel: str, chat_id: str) -> Session | None:
    """Load session from Postgres (CONTRACT §4).

    Returns None if not found or expired (session_store handles both cases).
    """
    return await get_session(channel, chat_id)


async def save_session_to_store(session: Session) -> None:
    """Persist session to Postgres with correct expires_at (CONTRACT §4, §7).

    Calculates expires_at from the state-specific TTL (or default 24h),
    stamps updated_at, then delegates to session_store.save_session().
    """
    settings = get_settings()

    session.update()  # stamps updated_at = now()

    state_timeout = get_timeout_seconds(session.state)
    ttl_seconds = state_timeout if state_timeout else settings.session_ttl_hours * 3600
    session.expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)

    await save_session(session)


async def create_session(
    trace_id: str | UUID | None,
    channel: str,
    chat_id: str,
    initial_state: ConversationState = ConversationState.IDLE,
) -> Session:
    """Create new session and persist it."""
    settings = get_settings()
    now = datetime.now(timezone.utc)

    if trace_id is None:
        trace_id_uuid = uuid4()
    elif isinstance(trace_id, str):
        trace_id_uuid = UUID(trace_id)
    else:
        trace_id_uuid = trace_id

    session = Session(
        trace_id=trace_id_uuid,
        channel=channel,
        chat_id=str(chat_id),
        state=initial_state,
        slots=SlotValues(),
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=settings.session_ttl_hours),
    )

    await save_session_to_store(session)
    return session


async def transition_state(
    session: Session,
    new_state: ConversationState,
) -> bool:
    """Transition session to new state (CONTRACT §7).

    Returns True if transition was allowed and persisted, False otherwise.
    """
    if not can_transition(session.state, new_state):
        return False

    session.state = new_state
    await save_session_to_store(session)
    return True


async def update_slots(
    session: Session,
    **slot_updates: str | datetime | list | None,
) -> None:
    """Update slot values in session and persist."""
    for key, value in slot_updates.items():
        if hasattr(session.slots, key):
            setattr(session.slots, key, value)
    await save_session_to_store(session)


async def check_timeout(session: Session) -> bool:
    """Return True if the session has exceeded its TTL (CONTRACT §7)."""
    now = datetime.now(timezone.utc)

    if session.expires_at < now:
        return True

    state_timeout = get_timeout_seconds(session.state)
    if state_timeout:
        elapsed = (now - session.updated_at).total_seconds()
        if elapsed > state_timeout:
            return True

    return False


async def reset_session(session: Session) -> None:
    """Reset session to IDLE, clearing all slots (CONTRACT §7)."""
    session.state = ConversationState.IDLE
    session.slots = SlotValues()
    await save_session_to_store(session)


async def get_or_create_session(
    trace_id: str | UUID | None,
    channel: str,
    chat_id: str,
) -> Session:
    """Return existing valid session, or create a fresh one.

    If the session is expired it is reset to IDLE before returning.
    The new trace_id is applied after reset so it propagates through
    the recovered session.
    """
    session = await load_session(channel, chat_id)

    if session is None:
        return await create_session(trace_id, channel, chat_id)

    if await check_timeout(session):
        await reset_session(session)
        if trace_id is not None:
            session.trace_id = UUID(trace_id) if isinstance(trace_id, str) else trace_id
            await save_session_to_store(session)

    return session


async def remove_session(channel: str, chat_id: str) -> None:
    """Hard-delete a session row (test teardown, explicit logout)."""
    await delete_session(channel, chat_id)


async def recover_stale_sessions() -> dict[str, int]:
    """Run on startup: find and fix stuck/expired sessions (CONTRACT §20).

    Returns counts of sessions handled per recovery category.
    """
    from app.storage.postgres import postgres_storage

    stats = {"booking_rescued": 0, "expired_reset": 0, "admin_timeout": 0}

    # 1. BOOKING_IN_PROGRESS > 1 min → reset to IDLE + notify
    stuck = await get_sessions_by_state(ConversationState.BOOKING_IN_PROGRESS, older_than_minutes=1)
    for session in stuck:
        await reset_session(session)
        stats["booking_rescued"] += 1

    # 2. Any non-IDLE state > 24h → IDLE (bulk SQL — cheaper than loading each row)
    result = await postgres_storage.execute(
        """
        UPDATE sessions SET
            fsm_state  = $1,
            slots      = '{}',
            updated_at = NOW(),
            expires_at = NOW() + INTERVAL '24 hours'
        WHERE fsm_state != $1
          AND updated_at < NOW() - INTERVAL '24 hours'
        """,
        ConversationState.IDLE.value,
    )
    # asyncpg returns 'UPDATE N' — parse the count
    try:
        stats["expired_reset"] = int(result.split()[-1])
    except (ValueError, IndexError, AttributeError):
        stats["expired_reset"] = 0

    # 3. ADMIN_RESPONDING > 4h → IDLE + notify
    admin_stuck = await get_sessions_by_state(
        ConversationState.ADMIN_RESPONDING, older_than_minutes=240
    )
    for session in admin_stuck:
        await reset_session(session)
        stats["admin_timeout"] += 1

    return stats
