"""Conversation session management.

Per CONTRACT §4, §7: Session load/save from Redis, state transitions, timeout rules.
"""

from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.core.fsm import can_transition, get_timeout_seconds, is_terminal_state
from app.models import ConversationState, Session, SlotValues
from app.storage.redis import redis_storage


def get_session_key(channel: str, chat_id: str) -> str:
    """Get Redis key for session.

    Args:
        channel: Channel name
        chat_id: Chat ID

    Returns:
        Redis key string
    """
    return f"session:{channel}:{chat_id}"


async def load_session(channel: str, chat_id: str) -> Session | None:
    """Load session from Redis (CONTRACT §4).

    Args:
        channel: Channel name
        chat_id: Chat ID

    Returns:
        Session if found, None otherwise
    """
    key = get_session_key(channel, chat_id)
    data = await redis_storage.get_json(key)

    if data is None:
        return None

    try:
        # Pydantic v2 handles datetime/enum/nested model conversion automatically
        return Session.model_validate(data)
    except Exception:
        # Invalid session data, return None
        return None


async def save_session(session: Session) -> None:
    """Save session to Redis with TTL (CONTRACT §4, §7).

    Args:
        session: Session to save
    """
    settings = get_settings()
    key = get_session_key(session.channel, session.chat_id)

    # Update timestamps
    session.update()

    # Calculate TTL based on state
    ttl_seconds: int | None = None

    # Check state-specific timeout
    state_timeout = get_timeout_seconds(session.state)
    if state_timeout:
        ttl_seconds = state_timeout
    else:
        # Default: session TTL from config (24h)
        ttl_seconds = settings.session_ttl_hours * 3600

    # Calculate expires_at
    session.expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)

    # Convert to dict for JSON serialization
    session_dict = session.model_dump(mode="json")

    # Save to Redis
    await redis_storage.set_json(key, session_dict, ex=ttl_seconds)


async def create_session(
    trace_id: str | None,
    channel: str,
    chat_id: str,
    initial_state: ConversationState = ConversationState.IDLE,
) -> Session:
    """Create new session.

    Args:
        trace_id: Trace ID (UUID string, optional - will generate if None)
        channel: Channel name
        chat_id: Chat ID
        initial_state: Initial FSM state

    Returns:
        New Session instance
    """
    from uuid import UUID, uuid4

    settings = get_settings()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=settings.session_ttl_hours)

    # Generate trace_id if not provided
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
        expires_at=expires_at,
    )

    await save_session(session)
    return session


async def transition_state(
    session: Session,
    new_state: ConversationState,
) -> bool:
    """Transition session to new state (CONTRACT §7).

    Args:
        session: Current session
        new_state: Target state

    Returns:
        True if transition successful, False if not allowed
    """
    if not can_transition(session.state, new_state):
        return False

    session.state = new_state
    await save_session(session)
    return True


async def update_slots(session: Session, **slot_updates: str | datetime | None) -> None:
    """Update slot values in session.

    Args:
        session: Session to update
        **slot_updates: Slot field updates (group, datetime_raw, datetime_resolved, etc.)
    """
    for key, value in slot_updates.items():
        if hasattr(session.slots, key):
            setattr(session.slots, key, value)

    await save_session(session)


async def check_timeout(session: Session) -> bool:
    """Check if session has timed out (CONTRACT §7).

    Args:
        session: Session to check

    Returns:
        True if timed out, False otherwise
    """
    now = datetime.now(timezone.utc)

    # Check expires_at
    if session.expires_at < now:
        return True

    # Check state-specific timeout
    state_timeout = get_timeout_seconds(session.state)
    if state_timeout:
        elapsed = (now - session.updated_at).total_seconds()
        if elapsed > state_timeout:
            return True

    return False


async def reset_session(session: Session) -> None:
    """Reset session to IDLE state (CONTRACT §7).

    Args:
        session: Session to reset
    """
    session.state = ConversationState.IDLE
    session.slots = SlotValues()
    await save_session(session)


async def get_or_create_session(
    trace_id: str | None,
    channel: str,
    chat_id: str,
) -> Session:
    """Get existing session or create new one.

    Args:
        trace_id: Trace ID (UUID string, optional - will generate if None)
        channel: Channel name
        chat_id: Chat ID

    Returns:
        Session instance
    """
    session = await load_session(channel, chat_id)

    if session is None:
        return await create_session(trace_id, channel, chat_id)

    # Check timeout
    if await check_timeout(session):
        # Reset expired session
        await reset_session(session)
        # Update trace_id to new one
        if trace_id is not None:
            from uuid import UUID

            session.trace_id = UUID(trace_id) if isinstance(trace_id, str) else trace_id
            await save_session(session)

    return session
