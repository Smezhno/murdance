"""Message filters for non-text content.

Per CONTRACT §8: Voice/sticker/image → friendly text reply.
Do NOT pass to LLM.
"""

from app.models import MessageType, UnifiedMessage


def is_text_message(message: UnifiedMessage) -> bool:
    """Check if message is text type.

    Args:
        message: UnifiedMessage to check

    Returns:
        True if text message, False otherwise
    """
    return message.message_type == "text"


def get_non_text_reply(message: UnifiedMessage) -> str:
    """Get friendly reply for non-text messages (CONTRACT §8).

    Args:
        message: UnifiedMessage with non-text type

    Returns:
        Friendly reply text
    """
    return "Я понимаю только текстовые сообщения 😊"


def should_process(message: UnifiedMessage) -> bool:
    """Check if message should be processed (text only).

    Args:
        message: UnifiedMessage to check

    Returns:
        True if should process, False if should filter out
    """
    return is_text_message(message)
