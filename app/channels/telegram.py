"""Telegram channel adapter using aiogram 3.x.

Per CONTRACT §8, §19: Webhook handler with signature verification.
"""

import hmac

from aiogram import Bot
from aiogram.types import Update
from fastapi import Request

from app.channels.base import ChannelProtocol
from app.channels.filters import get_non_text_reply
from app.config import get_settings
from app.models import MessageType, UnifiedMessage


class TelegramChannel:
    """Telegram channel adapter (CONTRACT §8, §19)."""

    def __init__(self) -> None:
        """Initialize Telegram bot."""
        settings = get_settings()
        self.bot = Bot(token=settings.telegram_bot_token)
        self.secret_token = settings.telegram_secret_token

    def verify_signature(self, request: Request) -> bool:
        """Verify Telegram webhook signature (CONTRACT §19).

        Telegram sends secret_token in X-Telegram-Bot-Api-Secret-Token header.

        Args:
            request: FastAPI Request object

        Returns:
            True if signature is valid, False otherwise
        """
        secret_token_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if not secret_token_header:
            return False
        return hmac.compare_digest(secret_token_header, self.secret_token)

    async def parse_webhook(self, request: Request) -> UnifiedMessage:
        """Parse Telegram webhook into UnifiedMessage (CONTRACT §8).

        Args:
            request: FastAPI Request object

        Returns:
            UnifiedMessage with Telegram data parsed

        Raises:
            ValueError: If webhook data is invalid
        """
        body_json = await request.json()
        update = Update(**body_json)

        if not update.message:
            raise ValueError("No message in Telegram update")

        msg = update.message
        user = msg.from_user

        # Determine message type
        message_type = MessageType.TEXT
        text = msg.text or ""
        if msg.voice:
            message_type = MessageType.VOICE
        elif msg.sticker:
            message_type = MessageType.STICKER
        elif msg.photo:
            message_type = MessageType.IMAGE

        # Extract sender info
        sender_name = None
        if user:
            sender_name = user.first_name
            if user.last_name:
                sender_name = f"{user.first_name} {user.last_name}"

        # msg.date is already a datetime object in aiogram 3.x
        timestamp = msg.date

        return UnifiedMessage(
            channel="telegram",
            chat_id=str(msg.chat.id),
            message_id=str(msg.message_id),
            timestamp=timestamp,
            text=text,
            message_type=message_type.value,
            sender_phone=None,  # Telegram doesn't provide phone
            sender_name=sender_name,
            raw_payload=update.model_dump(mode="json"),
        )

    async def send_message(self, chat_id: str, text: str) -> bool:
        """Send text message to Telegram chat.

        Telegram supports up to 4096 characters per message.

        Args:
            chat_id: Telegram chat ID
            text: Message text (truncated to 4096 chars if needed)

        Returns:
            True if sent successfully, False otherwise
        """
        try:
            # Truncate to 4096 chars (Telegram limit)
            if len(text) > 4096:
                text = text[:4093] + "..."

            await self.bot.send_message(chat_id=int(chat_id), text=text)
            return True
        except Exception:
            return False

    async def send_buttons(self, chat_id: str, text: str, buttons: list[dict]) -> bool:
        """Send message with inline keyboard buttons.

        Args:
            chat_id: Telegram chat ID
            text: Message text
            buttons: List of button dicts with 'text' and 'callback_data'

        Returns:
            True if sent successfully, False otherwise
        """
        try:
            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

            # Truncate text to 4096 chars (Telegram limit)
            if len(text) > 4096:
                text = text[:4093] + "..."

            keyboard_buttons = [
                [InlineKeyboardButton(text=btn["text"], callback_data=btn["callback_data"])]
                for btn in buttons
            ]
            keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

            await self.bot.send_message(chat_id=int(chat_id), text=text, reply_markup=keyboard)
            return True
        except Exception:
            return False

    async def send_typing(self, chat_id: str) -> None:
        """Send typing indicator.

        Args:
            chat_id: Telegram chat ID
        """
        try:
            await self.bot.send_chat_action(chat_id=int(chat_id), action="typing")
        except Exception:
            pass  # Ignore errors for typing indicator

    async def send_non_text_reply(self, chat_id: str, message: UnifiedMessage) -> None:
        """Send friendly reply for non-text messages.

        Args:
            chat_id: Telegram chat ID
            message: Non-text UnifiedMessage
        """
        reply_text = get_non_text_reply(message)
        await self.send_message(chat_id, reply_text)


# Lazy initialization pattern
_telegram_channel: TelegramChannel | None = None


def get_telegram_channel() -> TelegramChannel:
    """Get Telegram channel instance (lazy initialization)."""
    global _telegram_channel
    if _telegram_channel is None:
        _telegram_channel = TelegramChannel()
    return _telegram_channel
