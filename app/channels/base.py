"""Channel protocol interface.

Per RFC §16.1: Unified interface for all messaging channels.
"""

from typing import Protocol

from fastapi import Request

from app.models import UnifiedMessage


class ChannelProtocol(Protocol):
    """Protocol for channel adapters (CONTRACT §8, RFC §16.1)."""

    async def parse_webhook(self, request: Request) -> UnifiedMessage:
        """Parse webhook request into UnifiedMessage.

        Args:
            request: FastAPI Request object

        Returns:
            UnifiedMessage with channel-specific data parsed

        Raises:
            ValueError: If webhook data is invalid
        """
        ...

    async def send_message(self, chat_id: str, text: str) -> bool:
        """Send text message to chat.

        Args:
            chat_id: Chat ID (channel-specific)
            text: Message text (max length per channel)

        Returns:
            True if sent successfully, False otherwise
        """
        ...

    async def send_buttons(self, chat_id: str, text: str, buttons: list[dict]) -> bool:
        """Send message with inline buttons.

        Args:
            chat_id: Chat ID
            text: Message text
            buttons: List of button dictionaries (channel-specific format)

        Returns:
            True if sent successfully, False otherwise
        """
        ...

    async def send_typing(self, chat_id: str) -> None:
        """Send typing indicator.

        Args:
            chat_id: Chat ID
        """
        ...

    def verify_signature(self, request: Request) -> bool:
        """Verify webhook signature (CONTRACT §19).

        Args:
            request: FastAPI Request object

        Returns:
            True if signature is valid, False otherwise
        """
        ...
