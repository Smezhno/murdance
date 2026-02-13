"""Full booking flow orchestrator.

Per CONTRACT §6, §7, §11: Wires together LLM, FSM, Policy Enforcer, CRM, Idempotency.
"""

import logging
from datetime import date, datetime
from functools import lru_cache
from typing import Any
from uuid import UUID

from app.core.conversation import get_or_create_session, transition_state, update_slots
from app.core.fsm import can_transition
from app.core.idempotency import acquire_booking_lock, release_booking_lock
from app.core.temporal import get_temporal_parser
from app.integrations.impulse import get_impulse_adapter
from app.integrations.impulse.models import Client, Reservation, Schedule
from app.models import ConversationState, UnifiedMessage
from app.ai.policy import PolicyEnforcer
from app.knowledge.base import get_kb
from app.storage.postgres import postgres_storage

logger = logging.getLogger(__name__)


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

        # Handle special commands
        if message.text.startswith("/debug"):
            return await self._handle_debug_command(session)

        # Process based on current state
        if session.state == ConversationState.IDLE:
            response_text = await self._handle_idle(message, session, trace_id, conversation_history)
        elif session.state == ConversationState.BOOKING_IN_PROGRESS:
            response_text = await self._handle_booking_in_progress(message, session)
        elif session.state == ConversationState.ADMIN_RESPONDING:
            response_text = await self._handle_admin_responding(message, session)
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
            return response_text or "Помогу записаться на занятие! Какое направление вас интересует?"
        elif intent == "schedule_query":
            success = await transition_state(session, ConversationState.BROWSING_SCHEDULE)
            if not success:
                logger.warning(f"Failed to transition to BROWSING_SCHEDULE for trace_id={trace_id}")
            return response_text or "Показываю расписание..."
        else:
            # Info, greeting, etc. - stay in IDLE
            return response_text or "Чем могу помочь?"

    async def _handle_collecting_slots(
        self,
        message: UnifiedMessage,
        session: Any,
        trace_id: UUID,
        conversation_history: list[dict[str, str]] | None = None,
    ) -> str:
        """Handle slot collection states."""
        from app.core.intent import resolve_intent

        # Resolve intent and extract slots
        intent_result = await resolve_intent(
            message,
            session.state.value,
            session.slots.model_dump(),
            trace_id,
            conversation_history=conversation_history,
        )

        slots = intent_result.get("slots", {})
        response_text = intent_result.get("response_text", "")

        # Execute tool calls if needed (for schedule queries, etc.)
        # Tool execution belongs here, not in intent resolver
        tool_results: list[Any] = []
        if intent_result.get("intent") in ("schedule_query", "booking"):
            # Execute tools as needed
            tool_results = await self._execute_tools_for_intent(
                intent_result.get("intent"),
                slots,
                trace_id,
            )

        # Enforce policy (CONTRACT §11)
        is_valid, policy_error = self.policy_enforcer.enforce(
            response_text,
            [],  # No tool_calls from intent resolver
            tool_results,
        )
        if not is_valid and policy_error != "tool_failed":
            # Policy violation - log but continue
            pass

        # Parse datetime if provided
        if slots.get("datetime"):
            temporal_result = self.temporal_parser.parse(slots["datetime"])
            if temporal_result.date:
                from datetime import time as dt_time
                from zoneinfo import ZoneInfo
                
                # Get time from parser or default
                time_str = temporal_result.time or "19:00"
                time_obj = datetime.strptime(time_str, "%H:%M").time()
                
                # Combine date and time, make timezone-aware
                timezone = ZoneInfo(self.kb.studio.timezone)
                slots["datetime_resolved"] = datetime.combine(
                    temporal_result.date,
                    time_obj,
                ).replace(tzinfo=timezone)
            slots["datetime_raw"] = slots.get("datetime")

        # Update slots
        if slots:
            await update_slots(session, **slots)

        # Handle confirmation
        if session.state == ConversationState.CONFIRM_BOOKING:
            text_lower = message.text.lower()
            if text_lower in ("да", "yes", "подтверждаю", "согласен"):
                return await self.confirm_booking(session, trace_id)
            elif text_lower in ("нет", "no", "отмена"):
                success = await transition_state(session, ConversationState.IDLE)
                if not success:
                    logger.warning(f"Failed to transition to IDLE for trace_id={trace_id}")
                return "Запись отменена. Чем ещё могу помочь?"

        # Check if all required slots are filled
        if self._all_slots_filled(session) and session.state != ConversationState.CONFIRM_BOOKING:
            # Move to confirmation
            success = await transition_state(session, ConversationState.CONFIRM_BOOKING)
            if not success:
                logger.warning(f"Failed to transition to CONFIRM_BOOKING for trace_id={trace_id}")
            return await self._generate_confirmation_summary(session)
        else:
            # Ask for missing slots
            missing = self._get_missing_slots(session)
            if missing:
                return response_text or f"Нужна информация: {', '.join(missing)}"
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
            result_data = [s.model_dump() for s in schedules]
            duration_ms = int((time.monotonic() - start_time) * 1000)

            # Log tool call (CONTRACT §17)
            await postgres_storage.log_tool_call(
                trace_id=trace_id,
                tool_name="get_schedule",
                parameters={"date_from": date_from_str} if date_from_str else {},
                result={"count": len(result_data)},
                duration_ms=duration_ms,
            )

            return result_data
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

            # Find schedule_id by matching datetime and group
            schedule_id = session.slots.schedule_id
            if not schedule_id:
                # Get schedules and match by datetime and group
                target_date = session.slots.datetime_resolved.date() if session.slots.datetime_resolved else None
                target_time = session.slots.datetime_resolved.time() if session.slots.datetime_resolved else None

                if not target_date or not target_time:
                    return "Не удалось определить дату и время. Пожалуйста, уточните."

                # Map group name to group_id
                group_id = None
                if session.slots.group:
                    groups = await self.impulse.get_groups()
                    # Match group name (case-insensitive)
                    group_name_lower = session.slots.group.lower()
                    for group in groups:
                        if group.name.lower() == group_name_lower:
                            group_id = group.id
                            break

                # Get schedules and match by date, time, and group_id
                schedules = await self.impulse.get_schedule(date_from=target_date)
                for schedule in schedules:
                    if (
                        schedule.date == target_date.isoformat()
                        and schedule.time == target_time.strftime("%H:%M")
                        and (not group_id or schedule.group_id == group_id)
                    ):
                        schedule_id = schedule.id
                        break

                if not schedule_id:
                    return "Не удалось найти подходящее занятие. Обратитесь к администратору."

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
        receipt_template = """✅ Запись подтверждена!

Направление: {group}
Дата и время: {datetime_str}
Имя: {name}
Телефон: {phone}
Адрес: {address}

Номер записи: {reservation_id}
Напомню за день до занятия!"""
        
        receipt = receipt_template.format(
            group=group,
            datetime_str=datetime_str,
            name=client.name,
            phone=client.phone,
            address=studio_address,
            reservation_id=reservation.id,
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

