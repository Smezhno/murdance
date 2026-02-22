"""Full booking flow orchestrator.

Per CONTRACT §6, §7, §11: Wires together LLM, FSM, Policy Enforcer, CRM, Idempotency.
"""

import logging
from datetime import date, datetime
from functools import lru_cache
from typing import Any
from uuid import UUID

from app.core.cancel_flow import get_cancel_flow
from app.core.conversation import get_or_create_session, transition_state, update_slots
from app.core.fsm import can_transition
from app.core.idempotency import acquire_booking_lock, release_booking_lock
from app.core.temporal import get_temporal_parser
from app.integrations.impulse import get_impulse_adapter
from app.integrations.impulse.models import Client, Reservation, Schedule
from app.models import ConversationState, UnifiedMessage
from app.ai.policy import PolicyEnforcer
from app.ai.router import get_llm_router
from app.knowledge.base import get_kb
from app.storage.postgres import postgres_storage

logger = logging.getLogger(__name__)


def enforce_response_length(text: str, channel: str = "telegram") -> str:
    """Enforce response length per CONTRACT §6.

    Args:
        text: Response text
        channel: Channel name (telegram or whatsapp)

    Returns:
        Truncated text if needed, original text otherwise
    """
    max_length = {"telegram": 300, "whatsapp": 200}.get(channel, 300)
    if len(text) <= max_length:
        return text
    # Truncate at last sentence boundary before limit
    truncated = text[:max_length - 3]
    last_period = max(truncated.rfind("."), truncated.rfind("!"), truncated.rfind("?"))
    if last_period > max_length // 2:
        return truncated[:last_period + 1]
    return truncated + "..."


class BookingFlow:
    """Booking flow orchestrator (CONTRACT §6, §7, §11)."""

    def __init__(self) -> None:
        """Initialize booking flow."""
        self.impulse = get_impulse_adapter()
        self.policy_enforcer = PolicyEnforcer()
        self.temporal_parser = get_temporal_parser()
        self.kb = get_kb()

    async def process_message(
        self,
        message: UnifiedMessage,
        trace_id: UUID,
    ) -> str:
        """Process message through full booking flow.

        Args:
            message: User message
            trace_id: Trace ID

        Returns:
            Response text to send to user
        """
        # Get or create session
        session = await get_or_create_session(
            str(trace_id),
            message.channel,
            message.chat_id,
        )

        # Build conversation history from session slots (last 10 messages)
        conversation_history = session.slots.messages[-10:] if session.slots.messages else None

        # Guard: empty message
        if not message.text or not message.text.strip():
            return "Чем могу помочь? 😊"

        # Handle special commands
        if message.text.startswith("/debug"):
            return await self._handle_debug_command(session)

        # /start resets session to IDLE (CONTRACT §7)
        if message.text.strip() == "/start":
            await transition_state(session, ConversationState.IDLE)
            await update_slots(
                session,
                group=None,
                datetime_resolved=None,
                datetime_raw=None,
                client_name=None,
                client_phone=None,
                messages=[],
            )
            return "Здравствуйте! Чем могу помочь? 😊"

        # Process based on current state
        if session.state == ConversationState.CONFIRM_BOOKING:
            # Handle confirmation BEFORE any LLM call — avoids slot pollution from history
            text_lower = message.text.strip().lower()
            if text_lower in ("да", "yes", "подтверждаю", "согласен", "ок", "ok", "+"):
                response_text = await self.confirm_booking(session, trace_id)
            elif text_lower in ("нет", "no", "отмена", "cancel", "назад", "-"):
                await transition_state(session, ConversationState.IDLE)
                await update_slots(
                    session,
                    group=None,
                    datetime_resolved=None,
                    datetime_raw=None,
                    client_name=None,
                    client_phone=None,
                    messages=[],
                )
                response_text = "Запись отменена. Чем ещё могу помочь?"
            else:
                response_text = "Пожалуйста, ответьте «да» для подтверждения или «нет» для отмены записи."
        elif session.state == ConversationState.IDLE:
            response_text = await self._handle_idle(message, session, trace_id, conversation_history)
        elif session.state == ConversationState.BOOKING_IN_PROGRESS:
            response_text = await self._handle_booking_in_progress(message, session)
        elif session.state == ConversationState.ADMIN_RESPONDING:
            response_text = await self._handle_admin_responding(message, session)
        elif session.state == ConversationState.CANCEL_FLOW:
            cancel_flow = get_cancel_flow()
            # Route based on whether reservation is selected
            if session.slots.selected_reservation_id:
                response_text = await cancel_flow.confirm(session, message, trace_id)
            else:
                response_text = await cancel_flow.select(session, message, trace_id)
        else:
            response_text = await self._handle_collecting_slots(message, session, trace_id, conversation_history)

        # Update conversation history: append user message and bot response (keep last 10)
        if message.text and response_text:
            session.slots.messages.append({"role": "user", "content": message.text})
            session.slots.messages.append({"role": "assistant", "content": response_text})
            # Keep last 10 messages
            session.slots.messages = session.slots.messages[-10:]
            # Save updated slots
            await update_slots(session, messages=session.slots.messages)

        # Enforce response length for free-form LLM responses (CONTRACT §6)
        # Skip for structured messages: confirmation summary and receipt
        if session.state not in (
            ConversationState.CONFIRM_BOOKING,
            ConversationState.BOOKING_DONE,
            ConversationState.BOOKING_IN_PROGRESS,
            ConversationState.CANCEL_FLOW,  # Structured messages (list, confirmation)
        ):
            response_text = enforce_response_length(response_text, message.channel)

        return response_text

    async def _handle_idle(
        self,
        message: UnifiedMessage,
        session: Any,
        trace_id: UUID,
        conversation_history: list[dict[str, str]] | None = None,
    ) -> str:
        """Handle message in IDLE state."""
        from app.core.intent import resolve_intent

        # Keyword-based schedule detection — bypass LLM (CONTRACT §6, no hallucination)
        _SCHEDULE_KEYWORDS = (
            "расписание", "какое расписание", "покажи расписание",
            "какие занятия есть", "какие направления", "когда занятия",
            "во сколько занятия", "дни занятий", "есть ли занятия",
            "а какие есть", "какие есть", "что есть", "что у вас есть",
            "что там есть", "что за занятия", "какие стили", "какие варианты",
            "что вы предлагаете", "что предлагаете",
        )
        text_lower = message.text.lower().strip()
        if any(kw in text_lower for kw in _SCHEDULE_KEYWORDS):
            await transition_state(session, ConversationState.BROWSING_SCHEDULE)
            return await self._generate_schedule_response(
                session.slots.model_dump(), trace_id, conversation_history, message
            )

        # Resolve intent via LLM
        intent_result = await resolve_intent(
            message,
            session.state.value,
            session.slots.model_dump(),
            trace_id,
            conversation_history=conversation_history,
        )

        intent = intent_result.get("intent", "info")
        slots = intent_result.get("slots", {})
        response_text = intent_result.get("response_text", "")

        # Transition to appropriate state
        if intent == "booking":
            success = await transition_state(session, ConversationState.COLLECTING_INTENT)
            if not success:
                logger.warning(f"Failed to transition to COLLECTING_INTENT for trace_id={trace_id}")
            # Update slots from LLM extraction
            if slots:
                await update_slots(session, **slots)
            # Check if all slots filled → skip to confirmation (slot-skipping, CONTRACT §7)
            if self._all_slots_filled(session):
                success = await transition_state(session, ConversationState.CONFIRM_BOOKING)
                if not success:
                    logger.warning(f"Failed to transition to CONFIRM_BOOKING for trace_id={trace_id}")
                return await self._generate_confirmation_summary(session)
            # If direction known but no datetime — show schedule for that direction right away
            group = (slots.get("group") or session.slots.group)
            if group and not session.slots.datetime_resolved:
                schedule_reply = await self._generate_schedule_response(
                    {"group": group}, trace_id, conversation_history, message
                )
                return schedule_reply + "\n\nКакой день и время вам удобны?"
            return response_text or "Помогу записаться на занятие! Какое направление вас интересует?"
        elif intent == "schedule_query":
            success = await transition_state(session, ConversationState.BROWSING_SCHEDULE)
            if not success:
                logger.warning(f"Failed to transition to BROWSING_SCHEDULE for trace_id={trace_id}")
            return await self._generate_schedule_response(slots, trace_id, conversation_history, message)
        elif intent == "cancel":
            # Start cancel flow
            success = await transition_state(session, ConversationState.CANCEL_FLOW)
            if not success:
                logger.warning(f"Failed to transition to CANCEL_FLOW for trace_id={trace_id}")
            cancel_flow = get_cancel_flow()
            return await cancel_flow.start(session, trace_id)
        else:
            # Info, greeting, etc. - stay in IDLE
            return response_text or "Чем могу помочь?"

    async def _generate_schedule_response(
        self,
        slots: dict[str, Any],
        trace_id: UUID,
        conversation_history: list[dict[str, str]] | None,
        message: UnifiedMessage,
    ) -> str:
        """Fetch real schedule from CRM and generate response (CONTRACT §6).

        Groups schedules by (group_id, time, teacher) and shows days together:
        "Ср-Пт, 20:00 | Преподаватель: Даша"
        Never lets LLM invent schedule data — always uses CRM as source of truth.
        """
        schedule_data = await self._fetch_schedule(slots, trace_id)

        if isinstance(schedule_data, dict) and "error" in schedule_data:
            return "Не удалось получить расписание. Попробуйте позже или свяжитесь с администратором."

        if not schedule_data:
            return "На ближайшие дни занятий не найдено. Уточните у администратора."

        days_ru = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

        # Determine style filter from slots or message text
        group_filter: str | None = None
        slot_group = slots.get("group")
        if slot_group:
            slot_group_lower = slot_group.lower()
            for s in schedule_data:
                if hasattr(s, "style_name"):
                    if s.style_name.lower() == slot_group_lower or slot_group_lower in s.style_name.lower():
                        group_filter = s.style_name
                        break
        if not group_filter:
            user_q = message.text.strip().lower()
            for s in schedule_data:
                if hasattr(s, "style_name") and s.style_name.lower() in user_q:
                    group_filter = s.style_name
                    break

        filtered = [
            s for s in schedule_data
            if hasattr(s, "style_name") and (group_filter is None or s.style_name == group_filter)
        ]

        # Group by (group_id, minutes_begin, teacher_name) → collect days
        from collections import defaultdict
        groups: dict[tuple, list] = defaultdict(list)
        group_meta: dict[tuple, Any] = {}
        for s in filtered:
            key = (s.group_id, s.minutes_begin, s.teacher_name, s.style_name)
            groups[key].append(s.day)
            group_meta[key] = s

        def _fmt_group(key: tuple, days: list) -> str:
            s = group_meta[key]
            sorted_days = sorted(d for d in days if d is not None)
            days_str = "-".join(days_ru[d] for d in sorted_days) if sorted_days else "?"
            time_str = s.time_str
            teacher = s.teacher_name
            line = f"{days_str}, {time_str}"
            if teacher:
                line += f"\nПреподаватель: {teacher}"
            return line

        blocks = [_fmt_group(key, days) for key, days in groups.items()]

        if group_filter:
            header = f"Расписание {group_filter}:"
        else:
            header = "Актуальное расписание:"

        body = "\n\n".join(blocks)

        # Call to action depending on number of groups
        if len(groups) == 1:
            cta = "\n\nКакой день вам удобен?"
        elif len(groups) > 1:
            cta = "\n\nК кому хотели бы записаться?"
        else:
            cta = ""

        return f"{header}\n\n{body}{cta}"

    async def _handle_collecting_slots(
        self,
        message: UnifiedMessage,
        session: Any,
        trace_id: UUID,
        conversation_history: list[dict[str, str]] | None = None,
    ) -> str:
        """Handle slot collection states."""
        from app.core.intent import resolve_intent

        # Keyword-based schedule detection — bypass LLM (CONTRACT §6, no hallucination)
        # Only pure schedule inquiry phrases, not booking phrases with day/time info
        _SCHEDULE_KEYWORDS = (
            "расписание", "какое расписание", "покажи расписание",
            "какие занятия есть", "какие направления", "когда занятия",
            "во сколько занятия", "дни занятий", "есть ли занятия",
            "а какие есть", "какие есть", "что есть", "что у вас есть",
            "что там есть", "что за занятия", "какие стили", "какие варианты",
            "что вы предлагаете", "что предлагаете",
        )
        text_lower = message.text.lower().strip()
        if any(kw in text_lower for kw in _SCHEDULE_KEYWORDS):
            schedule_text = await self._generate_schedule_response(
                session.slots.model_dump(), trace_id, conversation_history, message
            )
            missing = self._get_missing_slots(session)
            if missing:
                follow_up = self._prompt_for_missing(missing, session)
                return f"{schedule_text}\n\n{follow_up}"
            return schedule_text

        # Resolve intent and extract slots
        intent_result = await resolve_intent(
            message,
            session.state.value,
            session.slots.model_dump(),
            trace_id,
            conversation_history=conversation_history,
        )

        intent = intent_result.get("intent", "")
        slots = intent_result.get("slots", {})
        response_text = intent_result.get("response_text", "")

        # If user asked about schedule mid-booking — show it, then continue collecting slots
        if intent == "schedule_query":
            schedule_text = await self._generate_schedule_response(slots, trace_id, conversation_history, message)
            missing = self._get_missing_slots(session)
            if missing:
                follow_up = self._prompt_for_missing(missing, session)
                return f"{schedule_text}\n\n{follow_up}"
            return schedule_text

        # Parse datetime if provided
        if slots.get("datetime"):
            temporal_result = self.temporal_parser.parse(slots["datetime"])
            if temporal_result.resolved_date:
                from datetime import time as dt_time
                from zoneinfo import ZoneInfo
                
                # Get time from parser or default
                time_str = temporal_result.time or "19:00"
                time_obj = datetime.strptime(time_str, "%H:%M").time()
                
                # Combine date and time, make timezone-aware
                timezone = ZoneInfo(self.kb.studio.timezone)
                slots["datetime_resolved"] = datetime.combine(
                    temporal_result.resolved_date,
                    time_obj,
                ).replace(tzinfo=timezone)
            slots["datetime_raw"] = slots.get("datetime")

        # Update slots — skip None values to avoid overwriting already-filled slots
        # (LLM returns null for fields not mentioned in current message)
        non_null_slots = {k: v for k, v in slots.items() if v is not None}
        if non_null_slots:
            await update_slots(session, **non_null_slots)

        # Validate teacher × day combination against real schedule
        teacher_validation = await self._validate_teacher_day(session, trace_id)
        if teacher_validation:
            return teacher_validation

        # Check if all required slots are filled
        if self._all_slots_filled(session) and session.state != ConversationState.CONFIRM_BOOKING:
            # Move to confirmation
            success = await transition_state(session, ConversationState.CONFIRM_BOOKING)
            if not success:
                logger.warning(f"Failed to transition to CONFIRM_BOOKING for trace_id={trace_id}")
            return await self._generate_confirmation_summary(session)
        else:
            # Ask for missing slots — use explicit prompts so LLM can't skip questions
            missing = self._get_missing_slots(session)
            if missing:
                return self._prompt_for_missing(missing, session)
            return response_text or "Чем ещё могу помочь?"

    async def _handle_booking_in_progress(
        self,
        message: UnifiedMessage,
        session: Any,
    ) -> str:
        """Handle message during booking (buffer)."""
        # Buffer message, don't process
        return "Идёт обработка записи, подождите немного..."

    async def _handle_admin_responding(
        self,
        message: UnifiedMessage,
        session: Any,
    ) -> str:
        """Handle message during admin handoff."""
        # Relay to admin (will be implemented in Phase 3)
        return "Ваше сообщение передано администратору."

    async def _handle_debug_command(self, session: Any) -> str:
        """Handle /debug command for testing."""
        return f"""Debug info:
State: {session.state.value}
Slots: {session.slots.model_dump()}
Trace ID: {session.trace_id}
Created: {session.created_at}
Updated: {session.updated_at}"""

    async def _execute_tools_for_intent(
        self,
        intent: str,
        slots: dict[str, Any],
        trace_id: UUID,
    ) -> list[Any]:
        """Execute tools for intent (tool execution belongs in booking_flow).

        Args:
            intent: Intent name
            slots: Current slots
            trace_id: Trace ID

        Returns:
            List of tool results
        """
        results = []
        if intent in ("schedule_query", "booking"):
            result = await self._fetch_schedule(slots, trace_id)
            results.append(result)
        return results

    async def _fetch_schedule(
        self,
        slots: dict[str, Any],
        trace_id: UUID,
    ) -> list[dict] | dict[str, str]:
        """Fetch schedule from CRM and log tool call.

        Args:
            slots: Current slots containing datetime
            trace_id: Trace ID for logging

        Returns:
            List of schedule dicts on success, {"error": str} on failure
        """
        import time
        from datetime import date

        start_time = time.monotonic()
        date_from_str = slots.get("datetime")
        
        try:
            # Parse date_from from slots["datetime"]
            date_from = None
            if date_from_str:
                try:
                    date_from = date.fromisoformat(date_from_str)
                except (ValueError, AttributeError):
                    # Invalid date format, will fetch all schedules
                    pass

            # Call CRM
            schedules = await self.impulse.get_schedule(date_from=date_from)
            duration_ms = int((time.monotonic() - start_time) * 1000)

            # Log tool call (CONTRACT §17)
            await postgres_storage.log_tool_call(
                trace_id=trace_id,
                tool_name="get_schedule",
                parameters={"date_from": date_from_str} if date_from_str else {},
                result={"count": len(schedules)},
                duration_ms=duration_ms,
            )

            return schedules
        except Exception as e:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            # Log tool call error (CONTRACT §17)
            await postgres_storage.log_tool_call(
                trace_id=trace_id,
                tool_name="get_schedule",
                parameters={"date_from": date_from_str} if date_from_str else {},
                error=str(e),
                duration_ms=duration_ms,
            )
            return {"error": str(e)}

    async def _validate_teacher_day(self, session: Any, trace_id: UUID) -> str | None:
        """Check that chosen teacher actually teaches on the selected day.

        Returns an error message string if the combination is invalid, None if OK.
        Only runs when both teacher and datetime_resolved are set.
        """
        teacher = session.slots.teacher
        dt = session.slots.datetime_resolved
        if not teacher or not dt:
            return None

        days_ru = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
        target_weekday = dt.weekday()
        teacher_lower = teacher.lower()

        try:
            schedules = await self.impulse.get_schedule()
        except Exception:
            return None  # Can't validate — let confirm_booking handle it

        # Find all schedules for this teacher
        teacher_schedules = [
            s for s in schedules
            if s.teacher_name and teacher_lower in s.teacher_name.lower()
        ]
        if not teacher_schedules:
            return None  # Unknown teacher — let LLM response stand

        # Check if teacher has a class on the requested day
        teacher_days = {s.day for s in teacher_schedules}
        if target_weekday not in teacher_days:
            available_days = sorted(teacher_days)
            available_str = "-".join(days_ru[d] for d in available_days if d is not None)
            # Clear the invalid datetime so the user can pick a valid day
            await update_slots(session, datetime_resolved=None, datetime_raw=None)
            return (
                f"У {teacher} нет занятий в {days_ru[target_weekday]}. "
                f"Доступные дни: {available_str}. Какой день вам удобен?"
            )

        # Also fix the time from the matched schedule (override LLM default)
        matching = next(
            (s for s in teacher_schedules if s.day == target_weekday), None
        )
        if matching and matching.minutes_begin is not None:
            from zoneinfo import ZoneInfo
            from datetime import time as dt_time
            tz = ZoneInfo(self.kb.studio.timezone)
            correct_time = dt_time(
                matching.minutes_begin // 60,
                matching.minutes_begin % 60,
            )
            corrected_dt = dt.replace(
                hour=correct_time.hour,
                minute=correct_time.minute,
                second=0,
                microsecond=0,
            )
            if corrected_dt != dt:
                await update_slots(session, datetime_resolved=corrected_dt)

        return None

    def _prompt_for_missing(self, missing: list[str], session: Any) -> str:
        """Return a deterministic question for the first missing slot.

        Asks one slot at a time in booking order: group → datetime → name+phone together.
        This prevents LLM from skipping questions (e.g. jumping to confirmation without asking date).
        """
        slots = session.slots

        if "направление" in missing:
            return "Какое направление вас интересует?"

        if "дата и время" in missing:
            # Build context-aware question using schedule if group is known
            group = slots.group or ""
            if group:
                return f"Когда вам удобно прийти на {group}? Укажите день недели или дату."
            return "Когда вам удобно прийти? Укажите день недели или дату."

        # Name and phone can be asked together
        if "имя" in missing and "телефон" in missing:
            return "Отлично! Назовите, пожалуйста, ваше имя и номер телефона для записи."
        if "имя" in missing:
            return "Как вас зовут?"
        if "телефон" in missing:
            return "Укажите, пожалуйста, ваш номер телефона."

        return "Уточните, пожалуйста, недостающие данные."

    def _all_slots_filled(self, session: Any) -> bool:
        """Check if all required booking slots are filled."""
        return (
            session.slots.group is not None
            and session.slots.datetime_resolved is not None
            and session.slots.client_name is not None
            and session.slots.client_phone is not None
        )

    def _get_missing_slots(self, session: Any) -> list[str]:
        """Get list of missing slot names."""
        missing = []
        if not session.slots.group:
            missing.append("направление")
        if not session.slots.datetime_resolved:
            missing.append("дата и время")
        if not session.slots.client_name:
            missing.append("имя")
        if not session.slots.client_phone:
            missing.append("телефон")
        return missing

    async def _generate_confirmation_summary(self, session: Any) -> str:
        """Generate booking confirmation summary (CONTRACT §6)."""
        slots = session.slots
        
        # Format datetime
        datetime_str = "не указано"
        if slots.datetime_resolved:
            datetime_str = slots.datetime_resolved.strftime("%d.%m.%Y %H:%M")
        elif slots.datetime_raw:
            datetime_str = slots.datetime_raw
        
        summary = f"""Подтвердите запись:

Направление: {slots.group or 'не указано'}
Дата и время: {datetime_str}
Имя: {slots.client_name or 'не указано'}
Телефон: {slots.client_phone or 'не указано'}

Подтверждаете? (да/нет)"""
        return summary

    async def confirm_booking(
        self,
        session: Any,
        trace_id: UUID,
    ) -> str:
        """Confirm and create booking (CONTRACT §6, §7, §10)."""
        # Check confirmation
        if not session.slots.group or not session.slots.datetime_resolved:
            return "Не все данные заполнены. Пожалуйста, укажите направление и дату."

        # Move to booking in progress
        await transition_state(session, ConversationState.BOOKING_IN_PROGRESS)

        try:
            # Find or create client
            client = await self.impulse.find_client(session.slots.client_phone)
            if not client:
                # Create new client
                client = await self.impulse.create_client(
                    name=session.slots.client_name or "Клиент",
                    phone=session.slots.client_phone,
                    trace_id=trace_id,
                )

            # Find schedule_id by matching day-of-week, time, and group name
            schedule_id = session.slots.schedule_id
            if not schedule_id:
                if not session.slots.datetime_resolved:
                    return "Не удалось определить дату и время. Пожалуйста, уточните."

                target_dt = session.slots.datetime_resolved
                # day_of_week: 0=Mon, 6=Sun (matches Impulse CRM day field)
                target_weekday = target_dt.weekday()
                target_minutes = target_dt.hour * 60 + target_dt.minute
                group_name_lower = (session.slots.group or "").lower()

                teacher_lower = (session.slots.teacher or "").lower()
                schedules = await self.impulse.get_schedule()
                for schedule in schedules:
                    day_match = schedule.day == target_weekday
                    # Allow ±30 min tolerance for time match
                    time_match = (
                        schedule.minutes_begin is not None
                        and abs(schedule.minutes_begin - target_minutes) <= 30
                    )
                    style_match = group_name_lower in schedule.style_name.lower() if group_name_lower else True
                    teacher_match = (
                        not teacher_lower
                        or (
                            schedule.teacher_name is not None
                            and teacher_lower in schedule.teacher_name.lower()
                        )
                    )
                    if day_match and time_match and style_match and teacher_match:
                        schedule_id = schedule.id
                        break

                if not schedule_id:
                    # Log and return user-friendly error
                    logger.warning(
                        f"No schedule found for weekday={target_weekday} "
                        f"time={target_minutes}min group={session.slots.group} trace_id={trace_id}"
                    )
                    return (
                        "Не нашла подходящего занятия в расписании. "
                        "Администратор свяжется с вами для подтверждения."
                    )

            # Check idempotency (CONTRACT §10)
            is_new, idempotency_msg = await acquire_booking_lock(
                session.slots.client_phone,
                schedule_id,
            )
            if not is_new:
                await transition_state(session, ConversationState.IDLE)
                return idempotency_msg

            try:
                # Create booking
                reservation = await self.impulse.create_booking(
                    client_id=client.id,
                    schedule_id=schedule_id,
                    booking_date=session.slots.datetime_resolved,
                    trace_id=trace_id,
                )

                # Success - move to done
                success = await transition_state(session, ConversationState.BOOKING_DONE)
                if not success:
                    logger.warning(f"Failed to transition to BOOKING_DONE for trace_id={trace_id}")

                # Generate receipt (CONTRACT §6)
                receipt = await self._generate_booking_receipt(reservation, client, session)

                # Log successful booking attempt (CONTRACT §17)
                await postgres_storage.log_booking_attempt(
                    trace_id=trace_id,
                    channel=session.channel,
                    chat_id=session.chat_id,
                    success=True,
                    group_id=session.slots.group,
                    schedule_id=str(schedule_id),
                    datetime=session.slots.datetime_resolved,
                    client_name=session.slots.client_name,
                    client_phone=session.slots.client_phone,
                )

                # Auto-transition to IDLE after 5s (handled by FSM timeout)
                return receipt

            except RuntimeError as e:
                # RuntimeError from error_handler has user-friendly message
                # Release lock on error
                await release_booking_lock(session.slots.client_phone, schedule_id)
                success = await transition_state(session, ConversationState.IDLE)
                if not success:
                    logger.warning(f"Failed to transition to IDLE after error for trace_id={trace_id}")
                # Log failed booking attempt (CONTRACT §17)
                await postgres_storage.log_booking_attempt(
                    trace_id=trace_id,
                    channel=session.channel,
                    chat_id=session.chat_id,
                    success=False,
                    group_id=session.slots.group,
                    schedule_id=str(schedule_id) if schedule_id else None,
                    client_name=session.slots.client_name,
                    client_phone=session.slots.client_phone,
                    error_message=str(e),
                )
                return str(e)
            except Exception as e:
                # Other exceptions - generic fallback
                # Release lock on error
                await release_booking_lock(session.slots.client_phone, schedule_id)
                success = await transition_state(session, ConversationState.IDLE)
                if not success:
                    logger.warning(f"Failed to transition to IDLE after error for trace_id={trace_id}")
                # Log failed booking attempt (CONTRACT §17)
                await postgres_storage.log_booking_attempt(
                    trace_id=trace_id,
                    channel=session.channel,
                    chat_id=session.chat_id,
                    success=False,
                    group_id=session.slots.group,
                    schedule_id=str(schedule_id) if schedule_id else None,
                    client_name=session.slots.client_name,
                    client_phone=session.slots.client_phone,
                    error_message=str(e),
                )
                return "Произошла ошибка при создании записи. Записал заявку — администратор подтвердит."

        except RuntimeError as e:
            # RuntimeError from error_handler has user-friendly message
            success = await transition_state(session, ConversationState.IDLE)
            if not success:
                logger.warning(f"Failed to transition to IDLE after error for trace_id={trace_id}")
            # Log failed booking attempt (CONTRACT §17)
            await postgres_storage.log_booking_attempt(
                trace_id=trace_id,
                channel=session.channel,
                chat_id=session.chat_id,
                success=False,
                group_id=session.slots.group,
                schedule_id=None,  # schedule_id not found yet
                client_name=session.slots.client_name,
                client_phone=session.slots.client_phone,
                error_message=str(e),
            )
            return str(e)
        except Exception as e:
            # Other exceptions - generic fallback
            success = await transition_state(session, ConversationState.IDLE)
            if not success:
                logger.warning(f"Failed to transition to IDLE after error for trace_id={trace_id}")
            # Log failed booking attempt (CONTRACT §17)
            await postgres_storage.log_booking_attempt(
                trace_id=trace_id,
                channel=session.channel,
                chat_id=session.chat_id,
                success=False,
                group_id=session.slots.group,
                schedule_id=None,  # schedule_id not found yet
                client_name=session.slots.client_name,
                client_phone=session.slots.client_phone,
                error_message=str(e),
            )
            return "Произошла ошибка при создании записи. Записал заявку — администратор подтвердит."

    async def _generate_booking_receipt(
        self,
        reservation: Reservation,
        client: Client,
        session: Any,
    ) -> str:
        """Generate booking receipt (CONTRACT §6).

        Per CONTRACT §6: receipt must include date/time/group/address.
        Response length: TG ≤ 300 chars.
        """
        # Get slots from session
        slots = session.slots
        group = slots.group or "не указано"
        
        # Format datetime
        datetime_str = "не указано"
        if slots.datetime_resolved:
            datetime_str = slots.datetime_resolved.strftime("%d.%m.%Y %H:%M")
        elif slots.datetime_raw:
            datetime_str = slots.datetime_raw
        
        # Get studio address from KB
        studio_address = self.kb.studio.address
        
        # Format receipt (keep under 300 chars per CONTRACT §6)
        reservation_line = f"\nНомер записи: {reservation.id}" if reservation.id else ""
        receipt_template = """✅ Запись подтверждена!

Направление: {group}
Дата и время: {datetime_str}
Имя: {name}
Телефон: {phone}
Адрес: {address}{reservation_line}
Напомню за день до занятия!"""
        
        receipt = receipt_template.format(
            group=group,
            datetime_str=datetime_str,
            name=client.name,
            phone=client.phone_str,
            address=studio_address,
            reservation_line=reservation_line,
        )
        
        # Ensure receipt is under 300 chars (CONTRACT §6)
        if len(receipt) > 300:
            # Truncate address if needed
            base_length = len(receipt) - len(studio_address)
            max_address_len = 300 - base_length - 3  # -3 for "..."
            if max_address_len > 0:
                truncated_address = studio_address[:max_address_len] + "..."
                receipt = receipt.replace(studio_address, truncated_address)
            else:
                # If even without address it's too long, truncate the whole receipt
                receipt = receipt[:297] + "..."
        
        return receipt


@lru_cache()
def get_booking_flow() -> BookingFlow:
    """Get BookingFlow instance (singleton)."""
    return BookingFlow()

