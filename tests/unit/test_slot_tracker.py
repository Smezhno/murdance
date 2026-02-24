"""Unit tests for slot_tracker: ConversationPhase and compute_phase().

Covers every phase transition, partial slots, slot-skipping, and backward
compatibility with existing SlotValues that lack the new RFC-003 fields.
"""

from datetime import datetime, timezone

import pytest

from app.core.slot_tracker import ConversationPhase, compute_phase
from app.models import SlotValues


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dt() -> datetime:
    return datetime(2026, 3, 1, 18, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Delegated flows — highest priority
# ---------------------------------------------------------------------------

class TestDelegatedFlows:
    def test_cancel_overrides_everything(self):
        slots = SlotValues(
            group="High Heels",
            branch="Семёновская",
            confirmed=True,
            booking_created=True,
        )
        assert compute_phase(slots, is_cancel=True) == ConversationPhase.CANCEL_FLOW

    def test_admin_overrides_when_no_cancel(self):
        slots = SlotValues(group="High Heels", confirmed=True)
        assert compute_phase(slots, is_admin=True) == ConversationPhase.ADMIN_HANDOFF

    def test_cancel_beats_admin(self):
        assert (
            compute_phase(SlotValues(), is_cancel=True, is_admin=True)
            == ConversationPhase.CANCEL_FLOW
        )


# ---------------------------------------------------------------------------
# POST_BOOKING
# ---------------------------------------------------------------------------

class TestPostBooking:
    def test_booking_created(self):
        slots = SlotValues(booking_created=True)
        assert compute_phase(slots) == ConversationPhase.POST_BOOKING

    def test_receipt_sent(self):
        slots = SlotValues(receipt_sent=True)
        assert compute_phase(slots) == ConversationPhase.POST_BOOKING

    def test_both_flags(self):
        slots = SlotValues(booking_created=True, receipt_sent=True)
        assert compute_phase(slots) == ConversationPhase.POST_BOOKING


# ---------------------------------------------------------------------------
# BOOKING
# ---------------------------------------------------------------------------

class TestBooking:
    def test_confirmed_true(self):
        slots = SlotValues(
            group="High Heels",
            branch="Семёновская",
            client_name="Маша",
            client_phone="89241234567",
            datetime_resolved=_dt(),
            confirmed=True,
        )
        assert compute_phase(slots) == ConversationPhase.BOOKING

    def test_confirmed_false_does_not_trigger(self):
        slots = SlotValues(confirmed=False)
        assert compute_phase(slots) != ConversationPhase.BOOKING


# ---------------------------------------------------------------------------
# CONFIRMATION
# ---------------------------------------------------------------------------

class TestConfirmation:
    def test_summary_shown(self):
        slots = SlotValues(summary_shown=True)
        assert compute_phase(slots) == ConversationPhase.CONFIRMATION

    def test_all_contact_slots_filled(self):
        slots = SlotValues(
            group="High Heels",
            client_name="Маша",
            client_phone="89241234567",
            datetime_resolved=_dt(),
        )
        assert compute_phase(slots) == ConversationPhase.CONFIRMATION

    def test_missing_group_does_not_trigger(self):
        slots = SlotValues(
            client_name="Маша",
            client_phone="89241234567",
            datetime_resolved=_dt(),
        )
        assert compute_phase(slots) != ConversationPhase.CONFIRMATION

    def test_missing_phone_does_not_trigger(self):
        slots = SlotValues(group="High Heels", client_name="Маша", datetime_resolved=_dt())
        assert compute_phase(slots) != ConversationPhase.CONFIRMATION

    def test_missing_name_does_not_trigger(self):
        slots = SlotValues(group="High Heels", client_phone="89241234567", datetime_resolved=_dt())
        assert compute_phase(slots) != ConversationPhase.CONFIRMATION

    def test_missing_datetime_does_not_trigger(self):
        slots = SlotValues(group="High Heels", client_name="Маша", client_phone="89241234567")
        assert compute_phase(slots) != ConversationPhase.CONFIRMATION


# ---------------------------------------------------------------------------
# COLLECTING_CONTACT
# ---------------------------------------------------------------------------

class TestCollectingContact:
    def test_datetime_resolved(self):
        slots = SlotValues(datetime_resolved=_dt())
        assert compute_phase(slots) == ConversationPhase.COLLECTING_CONTACT

    def test_schedule_shown_without_datetime(self):
        # RFC-003 §4.2 exact: schedule_shown alone → COLLECTING_CONTACT
        # TODO: validate with real conversations — may need datetime_resolved required here
        slots = SlotValues(schedule_shown=True)
        assert compute_phase(slots) == ConversationPhase.COLLECTING_CONTACT

    def test_schedule_shown_with_datetime(self):
        slots = SlotValues(schedule_shown=True, datetime_resolved=_dt())
        assert compute_phase(slots) == ConversationPhase.COLLECTING_CONTACT


# ---------------------------------------------------------------------------
# SCHEDULE
# ---------------------------------------------------------------------------

class TestSchedule:
    def test_branch_and_group(self):
        slots = SlotValues(branch="Семёновская", group="High Heels")
        assert compute_phase(slots) == ConversationPhase.SCHEDULE

    def test_branch_only_is_not_schedule(self):
        slots = SlotValues(branch="Семёновская")
        assert compute_phase(slots) == ConversationPhase.DISCOVERY

    def test_group_only_is_not_schedule(self):
        slots = SlotValues(group="High Heels")
        assert compute_phase(slots) == ConversationPhase.DISCOVERY


# ---------------------------------------------------------------------------
# DISCOVERY
# ---------------------------------------------------------------------------

class TestDiscovery:
    def test_branch_only(self):
        assert compute_phase(SlotValues(branch="Гоголя")) == ConversationPhase.DISCOVERY

    def test_group_only(self):
        assert compute_phase(SlotValues(group="Dancehall")) == ConversationPhase.DISCOVERY

    def test_experience_only(self):
        assert compute_phase(SlotValues(experience="новичок")) == ConversationPhase.DISCOVERY

    def test_experience_and_group(self):
        slots = SlotValues(experience="продолжающий", group="Frame Up Strip")
        assert compute_phase(slots) == ConversationPhase.DISCOVERY


# ---------------------------------------------------------------------------
# GREETING
# ---------------------------------------------------------------------------

class TestGreeting:
    def test_empty_slots(self):
        assert compute_phase(SlotValues()) == ConversationPhase.GREETING

    def test_only_teacher_filled(self):
        # teacher alone does not advance the phase
        assert compute_phase(SlotValues(teacher="Анна")) == ConversationPhase.GREETING

    def test_only_schedule_id_filled(self):
        assert compute_phase(SlotValues(schedule_id="abc123")) == ConversationPhase.GREETING

    def test_backward_compat_no_new_fields(self):
        """SlotValues created before RFC-003 (missing new fields) → GREETING."""
        slots = SlotValues(group=None, datetime_resolved=None, client_name=None)
        assert compute_phase(slots) == ConversationPhase.GREETING


# ---------------------------------------------------------------------------
# Slot-skipping: all contact fields at once bypasses intermediate phases
# ---------------------------------------------------------------------------

class TestSlotSkipping:
    def test_all_contact_fields_skips_to_confirmation(self):
        """User provides name + phone + datetime in one message → CONFIRMATION."""
        slots = SlotValues(
            group="High Heels",
            branch="Семёновская",
            client_name="Маша Иванова",
            client_phone="89241234567",
            datetime_resolved=_dt(),
        )
        assert compute_phase(slots) == ConversationPhase.CONFIRMATION

    def test_confirmed_skips_to_booking_regardless_of_other_flags(self):
        slots = SlotValues(
            group="High Heels",
            branch="Семёновская",
            client_name="Маша",
            client_phone="89241234567",
            datetime_resolved=_dt(),
            schedule_shown=True,
            summary_shown=True,
            confirmed=True,
        )
        assert compute_phase(slots) == ConversationPhase.BOOKING
