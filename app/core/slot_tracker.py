"""Slot tracker: ConversationPhase enum and compute_phase() function.

Phase is computed deterministically from SlotValues — never stored.
Replaces the FSM state machine per RFC-003 §4.2.
"""

from enum import Enum

from app.models import SlotValues


class ConversationPhase(str, Enum):
    """Conversation phase computed from slots on every request (RFC-003 §4.2).

    Not stored in the session — derived from SlotValues state.
    """

    GREETING = "greeting"
    DISCOVERY = "discovery"
    SCHEDULE = "schedule"
    COLLECTING_CONTACT = "contact"
    CONFIRMATION = "confirmation"
    BOOKING = "booking"
    POST_BOOKING = "post_booking"
    CANCEL_FLOW = "cancel_flow"
    ADMIN_HANDOFF = "admin_handoff"


def compute_phase(
    slots: SlotValues,
    is_cancel: bool = False,
    is_admin: bool = False,
) -> ConversationPhase:
    """Compute conversation phase from current slot state (RFC-003 §4.2).

    Priority order: delegated flows first, then booking progress top-to-bottom.
    First matching condition wins.
    """
    if is_cancel:
        return ConversationPhase.CANCEL_FLOW

    if is_admin:
        return ConversationPhase.ADMIN_HANDOFF

    if slots.booking_created or slots.receipt_sent:
        return ConversationPhase.POST_BOOKING

    if slots.confirmed:
        return ConversationPhase.BOOKING

    if slots.summary_shown or (
        slots.client_name and slots.client_phone and slots.datetime_resolved and slots.group
    ):
        return ConversationPhase.CONFIRMATION

    if slots.schedule_id and (slots.datetime_resolved or slots.schedule_shown):
        return ConversationPhase.CONFIRMATION

    if slots.datetime_resolved or slots.schedule_shown:
        # TODO: validate with real conversations — may need datetime_resolved required here
        # schedule_shown=True without datetime_resolved follows RFC-003 §4.2 exactly,
        # but a client who saw the schedule without picking a date is still in contact collection.
        return ConversationPhase.COLLECTING_CONTACT

    if slots.branch and slots.group:
        return ConversationPhase.SCHEDULE

    if slots.branch or slots.group or slots.experience:
        return ConversationPhase.DISCOVERY

    return ConversationPhase.GREETING
