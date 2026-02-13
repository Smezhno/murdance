"""CRM error handler: error codes → user-friendly messages.

Per CONTRACT §5: CRM errors → user-friendly message + fallback queue.
Per RFC §9.4: Error handling table.
"""

import httpx
from typing import Any


class ImpulseErrorHandler:
    """Error handler for Impulse CRM errors (CONTRACT §5, RFC §9.4)."""

    def handle_error(self, error: Exception, context: dict[str, Any] | None = None) -> tuple[str, bool]:
        """Handle CRM error and return user message.

        Args:
            error: Exception from CRM call
            context: Additional context (entity, action, etc.)

        Returns:
            Tuple of (user_message, should_fallback)
            should_fallback is True if error should go to fallback queue
        """
        # HTTP status errors
        if isinstance(error, httpx.HTTPStatusError):
            status = error.response.status_code
            if status >= 500:
                return (
                    "Технический сбой. Записал заявку — администратор подтвердит.",
                    True,
                )
            if status == 404:
                return (
                    "Расписание изменилось. Показать актуальное расписание?",
                    False,
                )
            if status in (400, 401, 403):
                return (
                    "Ошибка при обработке запроса. Попробуйте еще раз или обратитесь к администратору.",
                    False,
                )

        # Timeout errors
        if isinstance(error, httpx.TimeoutException):
            return (
                "Превышено время ожидания. Записал заявку — администратор подтвердит.",
                True,
            )

        # Circuit breaker open
        error_str = str(error).lower()
        if "circuit breaker" in error_str:
            return (
                "Сервис временно недоступен. Записал заявку — администратор подтвердит.",
                True,
            )

        # Specific CRM error codes (from RFC §9.4) - check error message
        if "нет мест" in error_str or "no seats" in error_str or "full" in error_str:
            return (
                "Нет мест на это время. Предлагаю ближайшие доступные варианты.",
                False,
            )

        if "уже записан" in error_str or "already booked" in error_str or "duplicate" in error_str:
            return (
                "Вы уже записаны на это занятие! Хотите записаться на другое время?",
                False,
            )

        if "занятие не найдено" in error_str or "not found" in error_str:
            return (
                "Расписание изменилось. Показать актуальное расписание?",
                False,
            )

        if "в прошлом" in error_str or "past" in error_str or "expired" in error_str:
            return (
                "Это время уже прошло. Предлагаю ближайшее доступное занятие.",
                False,
            )

        if "группа заполнена" in error_str or "group full" in error_str:
            return (
                "Группа полная. Хотите встать в лист ожидания или выбрать другое время?",
                False,
            )

        # Unknown error → fallback
        return (
            "Произошла ошибка. Записал заявку — администратор подтвердит.",
            True,
        )


# Global error handler instance
error_handler = ImpulseErrorHandler()

