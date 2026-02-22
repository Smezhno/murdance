"""Cancel booking flow handler.

Per CONTRACT §7: List future bookings → select → confirm → cancel.
"""

import logging
from datetime import date
from functools import lru_cache
from typing import Any
from uuid import UUID

from app.core.conversation import transition_state, update_slots
from app.integrations.impulse import get_impulse_adapter
from app.models import ConversationState, UnifiedMessage
from app.storage.redis import redis_storage

logger = logging.getLogger(__name__)


def _get_cancel_flow_key(channel: str, chat_id: str) -> str:
    """Get Redis key for cancel flow data."""
    return f"cancel_flow:{channel}:{chat_id}"


class CancelFlow:
    """Cancel booking flow handler (CONTRACT §7)."""

    def __init__(self) -> None:
        """Initialize cancel flow."""
        self.impulse = get_impulse_adapter()

    async def start(self, session: Any, trace_id: UUID) -> str:
        """Start cancel flow: find client, list bookings, show list.

        Args:
            session: Current session
            trace_id: Trace ID

        Returns:
            Response text with bookings list
        """
        # Get client phone
        client_phone = session.slots.client_phone or None
        if not client_phone:
            return "Для отмены записи нужен ваш номер телефона. Пожалуйста, укажите его."

        # Find client
        try:
            client = await self.impulse.find_client(client_phone)
            if not client:
                return "Не нашёл ваши записи. Проверьте номер телефона или обратитесь к администратору."
        except Exception as e:
            logger.error(f"Error finding client for cancel: {e}")
            return "Не удалось найти ваши записи. Обратитесь к администратору."

        # Get future bookings
        try:
            today = date.today()
            bookings = await self.impulse.list_bookings(client_id=client.id, date_from=today)

            if not bookings:
                await transition_state(session, ConversationState.IDLE)
                return "У вас нет предстоящих записей для отмены."

            # Fetch schedule and groups ONCE (fix N+1)
            schedules = await self.impulse.get_schedule(date_from=today)
            groups = await self.impulse.get_groups()

            # Build schedule lookup
            schedule_map = {s.id: s for s in schedules}
            group_map = {g.id: g for g in groups}

            # Filter and enrich bookings (store as plain dicts for JSON serialization)
            future_bookings = []
            for booking in bookings:
                schedule = schedule_map.get(booking.schedule_id)
                if schedule:
                    booking_date = date.fromisoformat(schedule.date)
                    if booking_date >= today:
                        group = group_map.get(schedule.group_id)
                        # Store as plain dict (JSON-serializable)
                        future_bookings.append({
                            "reservation_id": booking.id,
                            "group_name": group.name if group else "Неизвестно",
                            "date": schedule.date,
                            "time": schedule.time,
                        })

            if not future_bookings:
                await transition_state(session, ConversationState.IDLE)
                return "У вас нет предстоящих записей для отмены."

            # Store in Redis (TTL 10min)
            key = _get_cancel_flow_key(session.channel, session.chat_id)
            await redis_storage.set_json(key, future_bookings, ex=600)

            # Format list
            booking_list = []
            for idx, item in enumerate(future_bookings, 1):
                booking_date = date.fromisoformat(item["date"])
                booking_list.append(
                    f"{idx}. {item['group_name']}, {booking_date.strftime('%d.%m.%Y')} {item['time']}"
                )

            response = f"Ваши предстоящие записи:\n\n" + "\n".join(booking_list)
            response += "\n\nНапишите номер записи для отмены (например, '1')."
            return response

        except Exception as e:
            logger.error(f"Error in cancel flow start: {e}")
            await transition_state(session, ConversationState.IDLE)
            return "Произошла ошибка. Обратитесь к администратору."

    async def select(self, session: Any, message: UnifiedMessage, trace_id: UUID) -> str:
        """Parse user selection and show confirmation.

        Args:
            session: Current session
            message: User message
            trace_id: Trace ID

        Returns:
            Confirmation message
        """
        # Check for abort
        text_lower = message.text.lower().strip()
        if text_lower in ("нет", "no", "отмена", "выход"):
            key = _get_cancel_flow_key(session.channel, session.chat_id)
            await redis_storage.delete(key)
            await transition_state(session, ConversationState.IDLE)
            return "Хорошо. Чем ещё могу помочь?"

        # Load bookings from Redis
        key = _get_cancel_flow_key(session.channel, session.chat_id)
        future_bookings = await redis_storage.get_json(key)

        if not future_bookings:
            await transition_state(session, ConversationState.IDLE)
            return "Список записей устарел. Начните заново."

        # Parse selection
        selected_num = None

        try:
            if text_lower.isdigit():
                selected_num = int(text_lower)
            elif "отмена" in text_lower or "отменить" in text_lower:
                words = text_lower.split()
                for word in words:
                    if word.isdigit():
                        selected_num = int(word)
                        break
        except (ValueError, AttributeError):
            pass

        if selected_num is None or selected_num < 1:
            return "Не понял номер записи. Пожалуйста, укажите номер (например, '1')."

        if selected_num > len(future_bookings):
            return f"Нет записи с номером {selected_num}. Выберите из списка."

        # Get selected booking
        selected = future_bookings[selected_num - 1]
        reservation_id = selected["reservation_id"]

        # Store selected reservation ID
        await update_slots(session, selected_reservation_id=reservation_id)

        # Format confirmation (use plain dict fields)
        booking_date = date.fromisoformat(selected["date"])
        confirmation = (
            f"Отменить запись?\n\n"
            f"Направление: {selected['group_name']}\n"
            f"Дата: {booking_date.strftime('%d.%m.%Y')}\n"
            f"Время: {selected['time']}\n\n"
            f"Подтвердите отмену (да/нет)?"
        )
        return confirmation

    async def confirm(self, session: Any, message: UnifiedMessage, trace_id: UUID) -> str:
        """Execute cancellation.

        Args:
            session: Current session
            message: User message
            trace_id: Trace ID

        Returns:
            Cancellation result message
        """
        if not session.slots.selected_reservation_id:
            await transition_state(session, ConversationState.IDLE)
            return "Не выбрана запись для отмены. Начните заново."

        text_lower = message.text.lower()
        if text_lower not in ("да", "yes", "подтверждаю", "согласен"):
            if text_lower in ("нет", "no", "отмена"):
                await update_slots(session, selected_reservation_id=None)
                # Cleanup Redis key
                key = _get_cancel_flow_key(session.channel, session.chat_id)
                await redis_storage.delete(key)
                await transition_state(session, ConversationState.IDLE)
                return "Отмена записи отменена. Чем ещё могу помочь?"
            return "Пожалуйста, ответьте 'да' для подтверждения или 'нет' для отмены."

        # Cancel booking
        try:
            success = await self.impulse.cancel_booking(
                session.slots.selected_reservation_id,
                trace_id=trace_id,
            )

            # Cleanup
            await update_slots(session, selected_reservation_id=None)
            key = _get_cancel_flow_key(session.channel, session.chat_id)
            await redis_storage.delete(key)

            # Return to IDLE
            await transition_state(session, ConversationState.IDLE)

            if success:
                return "✅ Запись отменена. Если передумаете, можете записаться снова!"
            return "Не удалось отменить запись. Обратитесь к администратору."

        except Exception as e:
            logger.error(f"Error cancelling booking: {e}")
            await transition_state(session, ConversationState.IDLE)
            return "Произошла ошибка при отмене записи. Обратитесь к администратору."


@lru_cache()
def get_cancel_flow() -> CancelFlow:
    """Get CancelFlow instance (singleton)."""
    return CancelFlow()

