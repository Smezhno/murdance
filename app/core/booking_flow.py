"""Booking flow orchestrator — thin router (CONTRACT §6, §7, §11).

Delegates to:
  schedule_flow   — schedule fetching and formatting
  slot_collector  — slot extraction and validation
  booking_confirm — CRM booking creation and receipt
  response_generator — natural LLM-generated responses
"""

import logging
from functools import lru_cache
from typing import Any
from uuid import UUID

from app.core.booking_confirm import confirm_booking, generate_confirmation_summary
from app.core.cancel_flow import get_cancel_flow
from app.core.conversation import get_or_create_session, transition_state, update_slots
from app.core.response_generator import generate_response
from app.core.schedule_flow import generate_schedule_response, is_schedule_query
from app.core.slot_collector import _ask_next_slot, all_slots_filled, collect_slots, get_missing_slots
from app.integrations.impulse import get_impulse_adapter
from app.core.temporal import get_temporal_parser
from app.knowledge.base import get_kb
from app.models import ConversationState, UnifiedMessage

logger = logging.getLogger(__name__)


def enforce_response_length(text: str, channel: str = "telegram") -> str:
    """Truncate response to channel limit (CONTRACT §6)."""
    max_length = {"telegram": 300, "whatsapp": 200}.get(channel, 300)
    if len(text) <= max_length:
        return text
    truncated = text[:max_length - 3]
    last_period = max(truncated.rfind("."), truncated.rfind("!"), truncated.rfind("?"))
    if last_period > max_length // 2:
        return truncated[:last_period + 1]
    return truncated + "..."


class BookingFlow:
    """Thin routing orchestrator — delegates all heavy logic to sub-modules."""

    def __init__(self) -> None:
        self.impulse = get_impulse_adapter()
        self.temporal_parser = get_temporal_parser()
        self._kb: Any = None

    @property
    def kb(self) -> Any:
        if self._kb is None:
            self._kb = get_kb()
        return self._kb

    async def process_message(self, message: UnifiedMessage, trace_id: UUID) -> str:
        session = await get_or_create_session(
            str(trace_id), message.channel, message.chat_id
        )
        history = session.slots.messages[-10:] or None

        # Guards
        if not message.text or not message.text.strip():
            return await generate_response("unclear", {}, trace_id)

        if message.text.strip() == "/start":
            await self._reset_session(session)
            return await generate_response("greet", {}, trace_id)

        if message.text.startswith("/debug"):
            return self._handle_debug_command(session)

        # State routing
        state = session.state
        if state == ConversationState.CONFIRM_BOOKING:
            response_text = await self._handle_confirmation(message, session, trace_id, history)
        elif state == ConversationState.IDLE:
            response_text = await self._handle_idle(message, session, trace_id, history)
        elif state == ConversationState.BOOKING_IN_PROGRESS:
            response_text = "Подожди немного, оформляю запись... ⏳"
        elif state == ConversationState.ADMIN_RESPONDING:
            response_text = "Передала твоё сообщение администратору."
        elif state == ConversationState.CANCEL_FLOW:
            cancel_flow = get_cancel_flow()
            if session.slots.selected_reservation_id:
                response_text = await cancel_flow.confirm(session, message, trace_id)
            else:
                response_text = await cancel_flow.select(session, message, trace_id)
        elif state in (ConversationState.BOOKING_DONE, ConversationState.BROWSING_SCHEDULE):
            await transition_state(session, ConversationState.IDLE)
            response_text = await self._handle_idle(message, session, trace_id, history)
        else:
            # COLLECTING_* states
            response_text = await collect_slots(
                message, session, trace_id, history,
                self.impulse, self.temporal_parser, self.kb,
            )
            # Transition to confirmation if all slots filled after collection
            if all_slots_filled(session) and session.state != ConversationState.CONFIRM_BOOKING:
                await transition_state(session, ConversationState.CONFIRM_BOOKING)
                response_text = generate_confirmation_summary(session)

        # Save conversation history (keep last 10 messages)
        if message.text and response_text:
            session.slots.messages.append({"role": "user", "content": message.text})
            session.slots.messages.append({"role": "assistant", "content": response_text})
            session.slots.messages = session.slots.messages[-10:]
            await update_slots(session, messages=session.slots.messages)

        # Enforce response length for free-form responses (skip structured ones)
        if session.state not in (
            ConversationState.CONFIRM_BOOKING,
            ConversationState.BOOKING_DONE,
            ConversationState.BOOKING_IN_PROGRESS,
            ConversationState.CANCEL_FLOW,
        ):
            response_text = enforce_response_length(response_text, message.channel)

        return response_text

    async def _handle_confirmation(
        self,
        message: UnifiedMessage,
        session: Any,
        trace_id: UUID,
        history: list | None,
    ) -> str:
        """Handle CONFIRM_BOOKING state with fast-path yes/no, LLM fallback for ambiguous input."""
        from app.core.intent import resolve_intent

        text_lower = message.text.strip().lower()

        # Fast path — obvious yes
        if text_lower in ("да", "yes", "ок", "ok", "+", "подтверждаю", "подтверждаем"):
            return await confirm_booking(session, trace_id, self.impulse, self.kb)

        # Fast path — obvious no
        if text_lower in ("нет", "no", "-", "отмена", "cancel"):
            await self._reset_session(session)
            return await generate_response("booking_cancelled", {}, trace_id, history)

        # Ambiguous — ask LLM to classify
        intent_result = await resolve_intent(
            message, "confirm_booking", session.slots.model_dump(), trace_id,
            conversation_history=history,
        )
        intent = intent_result.get("intent", "unclear")
        slots = intent_result.get("slots", {})

        if intent == "booking":
            # Guard: if LLM extracted new group/datetime, user wants a different booking —
            # don't confirm current one; reset and start fresh.
            new_group = slots.get("group")
            new_datetime = slots.get("datetime")
            if new_group or new_datetime:
                await self._reset_session(session)
                non_null = {k: v for k, v in slots.items() if v is not None}
                if non_null:
                    await update_slots(session, **non_null)
                await transition_state(session, ConversationState.COLLECTING_INTENT)
                return await generate_response("ask_direction", {}, trace_id, history)
            return await confirm_booking(session, trace_id, self.impulse, self.kb)

        if intent in ("cancel",):
            await self._reset_session(session)
            return await generate_response("booking_cancelled", {}, trace_id, history)

        # Still unclear — re-show summary
        return generate_confirmation_summary(session)

    async def _handle_idle(
        self,
        message: UnifiedMessage,
        session: Any,
        trace_id: UUID,
        history: list | None,
    ) -> str:
        """Handle IDLE state: schedule queries, booking intent, info."""
        from app.core.intent import resolve_intent

        slots_dict = session.slots.model_dump()

        # Deterministic schedule query — no LLM
        if is_schedule_query(message.text):
            await transition_state(session, ConversationState.BROWSING_SCHEDULE)
            return await generate_schedule_response(
                self.impulse, slots_dict, trace_id, message.text
            )

        intent_result = await resolve_intent(
            message, session.state.value, slots_dict, trace_id,
            conversation_history=history,
        )
        intent = intent_result.get("intent", "info")
        slots = intent_result.get("slots", {})

        if intent == "booking":
            await transition_state(session, ConversationState.COLLECTING_INTENT)
            non_null = {k: v for k, v in slots.items() if v is not None}
            if non_null:
                await update_slots(session, **non_null)

            # If direction already known — show its schedule right away (no second LLM call)
            # But only if teacher is not yet known — otherwise go straight to collect_slots
            group = slots.get("group") or session.slots.group
            teacher = slots.get("teacher") or session.slots.teacher
            if group and not session.slots.datetime_resolved and not teacher:
                sched = await generate_schedule_response(
                    self.impulse, {"group": group}, trace_id, message.text
                )
                return f"{sched}\n\nК кому хочешь записаться? 😊"

            if all_slots_filled(session):
                await transition_state(session, ConversationState.CONFIRM_BOOKING)
                return generate_confirmation_summary(session)

            # Group known — ask for the next missing slot (datetime / contact)
            if session.slots.group:
                missing = get_missing_slots(session)
                return await _ask_next_slot(missing, session, trace_id, history)

            # Teacher known but group unknown — look up group from CRM schedule
            if session.slots.teacher and not session.slots.group:
                try:
                    schedules = await self.impulse.get_schedule()
                    teacher_lower = session.slots.teacher.lower()
                    for s in schedules:
                        t_name = getattr(s, "teacher_name", None) or ""
                        if teacher_lower in t_name.lower():
                            group_name = getattr(s, "style_name", None)
                            if group_name:
                                await update_slots(session, group=group_name)
                                missing = get_missing_slots(session)
                                return await _ask_next_slot(missing, session, trace_id, history)
                            break
                except Exception:
                    pass

            return await generate_response("ask_direction", {}, trace_id, history)

        if intent == "schedule_query":
            await transition_state(session, ConversationState.BROWSING_SCHEDULE)
            return await generate_schedule_response(
                self.impulse, slots, trace_id, message.text
            )

        if intent == "cancel":
            await transition_state(session, ConversationState.CANCEL_FLOW)
            return await get_cancel_flow().start(session, trace_id)

        if intent in ("unsure", "help"):
            return await generate_response("suggest_trial", {}, trace_id, history)

        # Greeting — use studio-tone LLM response
        if intent == "greeting":
            return await generate_response("greet", {}, trace_id, history)

        # Info — use LLM-generated response from resolve_intent
        return intent_result.get("response_text") or await generate_response("greet", {}, trace_id, history)

    async def _reset_session(self, session: Any) -> None:
        """Reset all slots and transition to IDLE."""
        await transition_state(session, ConversationState.IDLE)
        await update_slots(
            session,
            group=None, datetime_resolved=None, datetime_raw=None,
            client_name=None, client_phone=None, teacher=None,
            schedule_id=None, messages=[],
        )

    def _handle_debug_command(self, session: Any) -> str:
        return (
            f"Debug info:\n"
            f"State: {session.state.value}\n"
            f"Slots: {session.slots.model_dump()}\n"
            f"Trace ID: {session.trace_id}"
        )


@lru_cache()
def get_booking_flow() -> BookingFlow:
    """Get BookingFlow singleton."""
    return BookingFlow()
