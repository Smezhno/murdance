"""Cancel booking flow handler.

Per CONTRACT §7: List future bookings → select → confirm → cancel.

Cancel-flow bookings list is stored in session.slots metadata via update_slots()
(key: cancel_bookings) instead of the former Redis temporary key.
TTL is implicitly bounded by the session TTL (CANCEL_FLOW state = 24h).
"""

import logging
from datetime import date
from functools import lru_cache
from typing import Any
from uuid import UUID

from app.core.conversation import transition_state, update_slots
from app.integrations.impulse import get_impulse_adapter
from app.models import ConversationState, UnifiedMessage

logger = logging.getLogger(__name__)


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

            # Build booking list from nested data already present in Reservation objects
            future_bookings = []
            for booking in bookings:
                booking_date = booking.date_as_date
                if booking_date and booking_date >= today:
                    future_bookings.append({
                        "reservation_id": booking.id,
                        "group_name": booking.group_name,
                        "date": booking_date.isoformat(),
                        "time": booking.time_str,
                    })

            if not future_bookings:
                await transition_state(session, ConversationState.IDLE)
                return "У вас нет предстоящих записей для отмены."

            # Store in session slots (persists until session TTL expires)
            await update_slots(session, cancel_bookings=future_bookings)
            await transition_state(session, ConversationState.CANCEL_FLOW)

            # Single booking — skip selection, go straight to confirmation
            if len(future_bookings) == 1:
                item = future_bookings[0]
                await update_slots(session, selected_reservation_id=item["reservation_id"])
                booking_date = date.fromisoformat(item["date"])
                return (
                    f"Точно отменяем?\n\n"
                    f"{item['group_name']}, {booking_date.strftime('%d.%m.%Y')} в {item['time']}\n\n"
                    f"Напиши «да» для подтверждения или «нет» для отмены."
                )

            # Multiple bookings — show list
            booking_list = []
            for idx, item in enumerate(future_bookings, 1):
                booking_date = date.fromisoformat(item["date"])
                booking_list.append(
                    f"{idx}. {item['group_name']}, {booking_date.strftime('%d.%m.%Y')} в {item['time']}"
                )

            response = "Какую запись отменить?\n\n" + "\n".join(booking_list)
            response += "\n\nНапиши цифру или название занятия."
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
            await update_slots(session, cancel_bookings=[])
            await transition_state(session, ConversationState.IDLE)
            return "Хорошо. Чем ещё могу помочь?"

        # Load bookings from session slots — if empty, restart the flow
        future_bookings = session.slots.cancel_bookings

        if not future_bookings:
            return await self.start(session, trace_id)

        # Parse selection — accept digit or partial name match
        selected: dict | None = None

        try:
            if text_lower.isdigit():
                idx = int(text_lower) - 1
                if 0 <= idx < len(future_bookings):
                    selected = future_bookings[idx]
            else:
                # Match by group name substring
                for item in future_bookings:
                    if text_lower in item["group_name"].lower():
                        selected = item
                        break
                # Also try to extract digit from phrase like "первую" or "отменить 2"
                if not selected:
                    for word in text_lower.split():
                        if word.isdigit():
                            idx = int(word) - 1
                            if 0 <= idx < len(future_bookings):
                                selected = future_bookings[idx]
                            break
        except (ValueError, AttributeError):
            pass

        if selected is None:
            booking_list = []
            for idx, item in enumerate(future_bookings, 1):
                booking_date = date.fromisoformat(item["date"])
                booking_list.append(f"{idx}. {item['group_name']}, {booking_date.strftime('%d.%m.%Y')} в {item['time']}")
            return "Не поняла, какую запись отменить. Напиши цифру:\n\n" + "\n".join(booking_list)

        # Store selected reservation ID
        await update_slots(session, selected_reservation_id=selected["reservation_id"])

        booking_date = date.fromisoformat(selected["date"])
        return (
            f"Точно отменяем?\n\n"
            f"{selected['group_name']}, {booking_date.strftime('%d.%m.%Y')} в {selected['time']}\n\n"
            f"Напиши «да» для подтверждения или «нет» для отмены."
        )

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
                await update_slots(session, selected_reservation_id=None, cancel_bookings=[])
                await transition_state(session, ConversationState.IDLE)
                return "Отмена записи отменена. Чем ещё могу помочь?"
            return "Пожалуйста, ответьте 'да' для подтверждения или 'нет' для отмены."

        # Cancel booking
        try:
            success = await self.impulse.cancel_booking(
                session.slots.selected_reservation_id,
                trace_id=trace_id,
            )

            # Cleanup slots
            await update_slots(session, selected_reservation_id=None, cancel_bookings=[])

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

