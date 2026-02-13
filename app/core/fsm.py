"""FSM state machine logic.

Per CONTRACT §7: Deterministic FSM with slot filling.
ConversationState enum is defined in app.models.
"""

from app.models import ConversationState


def can_transition(from_state: ConversationState, to_state: ConversationState) -> bool:
    """Check if state transition is allowed (CONTRACT §7).

    Args:
        from_state: Current state
        to_state: Target state

    Returns:
        True if transition is allowed, False otherwise
    """
    # Define allowed transitions per CONTRACT §7
    transitions: dict[ConversationState, list[ConversationState]] = {
        ConversationState.IDLE: [
            ConversationState.COLLECTING_INTENT,
            ConversationState.CANCEL_FLOW,
            ConversationState.HANDOFF_TO_ADMIN,
        ],
        ConversationState.COLLECTING_INTENT: [
            ConversationState.BROWSING_SCHEDULE,
            ConversationState.COLLECTING_GROUP,
            ConversationState.COLLECTING_DATETIME,  # Slot-skipping: user provides datetime directly
            ConversationState.COLLECTING_CONTACT,  # Slot-skipping: user provides contact directly
            ConversationState.IDLE,
            ConversationState.CANCEL_FLOW,
            ConversationState.HANDOFF_TO_ADMIN,
        ],
        ConversationState.BROWSING_SCHEDULE: [
            ConversationState.COLLECTING_GROUP,
            ConversationState.IDLE,
            ConversationState.CANCEL_FLOW,
            ConversationState.HANDOFF_TO_ADMIN,
        ],
        ConversationState.COLLECTING_GROUP: [
            ConversationState.COLLECTING_DATETIME,
            ConversationState.IDLE,
            ConversationState.CANCEL_FLOW,
            ConversationState.HANDOFF_TO_ADMIN,
        ],
        ConversationState.COLLECTING_DATETIME: [
            ConversationState.COLLECTING_CONTACT,
            ConversationState.IDLE,
            ConversationState.CANCEL_FLOW,
            ConversationState.HANDOFF_TO_ADMIN,
        ],
        ConversationState.COLLECTING_CONTACT: [
            ConversationState.CONFIRM_BOOKING,
            ConversationState.IDLE,
            ConversationState.CANCEL_FLOW,
            ConversationState.HANDOFF_TO_ADMIN,
        ],
        ConversationState.CONFIRM_BOOKING: [
            ConversationState.BOOKING_IN_PROGRESS,
            ConversationState.IDLE,
            ConversationState.CANCEL_FLOW,
            ConversationState.HANDOFF_TO_ADMIN,
        ],
        ConversationState.BOOKING_IN_PROGRESS: [
            ConversationState.BOOKING_DONE,
            ConversationState.IDLE,  # On timeout/error
            ConversationState.HANDOFF_TO_ADMIN,
        ],
        ConversationState.BOOKING_DONE: [
            ConversationState.IDLE,  # Auto-transition after 5s
            ConversationState.SERIAL_BOOKING,  # User wants to book more classes
        ],
        ConversationState.CANCEL_FLOW: [
            ConversationState.IDLE,
            ConversationState.HANDOFF_TO_ADMIN,
        ],
        ConversationState.SERIAL_BOOKING: [
            ConversationState.COLLECTING_GROUP,  # Start new booking in batch
            ConversationState.IDLE,
            ConversationState.HANDOFF_TO_ADMIN,
        ],
        ConversationState.HANDOFF_TO_ADMIN: [
            ConversationState.ADMIN_RESPONDING,
            ConversationState.IDLE,
        ],
        ConversationState.ADMIN_RESPONDING: [
            ConversationState.IDLE,
        ],
    }

    allowed = transitions.get(from_state, [])
    return to_state in allowed


def get_timeout_seconds(state: ConversationState) -> int | None:
    """Get timeout in seconds for a state (CONTRACT §7).

    Args:
        state: Conversation state

    Returns:
        Timeout in seconds, or None if no timeout
    """
    # Timeouts per CONTRACT §7
    timeouts: dict[ConversationState, int] = {
        ConversationState.CONFIRM_BOOKING: 3 * 3600,  # 3h → IDLE
        ConversationState.BOOKING_IN_PROGRESS: 30,  # 30s → fallback
        ConversationState.ADMIN_RESPONDING: 4 * 3600,  # 4h → IDLE
    }
    return timeouts.get(state)


def is_terminal_state(state: ConversationState) -> bool:
    """Check if state is terminal (auto-transitions).

    Args:
        state: Conversation state

    Returns:
        True if terminal state
    """
    return state == ConversationState.BOOKING_DONE


def is_persistent_state(state: ConversationState) -> bool:
    """Check if state is persistent (long-lived).

    Args:
        state: Conversation state

    Returns:
        True if persistent state
    """
    return state in (
        ConversationState.HANDOFF_TO_ADMIN,
        ConversationState.ADMIN_RESPONDING,
    )
