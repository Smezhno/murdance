"""Fallback queue for CRM errors.

Per CONTRACT §5: Redis fallback queue + TG admin alert.
"""

import json
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any
from uuid import uuid4

from app.channels.telegram import get_telegram_channel
from app.config import get_settings
from app.storage.redis import redis_storage


class ImpulseFallback:
    """Fallback queue for CRM errors (CONTRACT §5)."""

    QUEUE_KEY = "impulse:fallback:queue"

    def __init__(self) -> None:
        """Initialize fallback queue."""
        self.settings = get_settings()
        self.admin_chat_id = self.settings.admin_telegram_chat_id

    async def enqueue(
        self,
        action: str,
        data: dict[str, Any],
        error: str,
        trace_id: str | None = None,
    ) -> None:
        """Enqueue booking request to fallback queue.

        Args:
            action: Action name (create_booking, create_client, etc.)
            data: Request data
            error: Error message
            trace_id: Optional trace ID
        """
        if trace_id is None:
            trace_id = str(uuid4())

        item = {
            "id": str(uuid4()),
            "trace_id": trace_id,
            "action": action,
            "data": data,
            "error": error,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        # Add to Redis list (left push for FIFO)
        await redis_storage.lpush(self.QUEUE_KEY, json.dumps(item, default=str))

        # Send alert to admin Telegram
        await self._send_admin_alert(item)

    async def _send_admin_alert(self, item: dict[str, Any]) -> None:
        """Send alert to admin Telegram.

        Args:
            item: Fallback queue item
        """
        try:
            telegram_channel = get_telegram_channel()
            message = (
                f"⚠️ CRM Fallback Queue\n\n"
                f"Action: {item['action']}\n"
                f"Error: {item['error']}\n"
                f"Trace ID: {item['trace_id']}\n"
                f"Created: {item['created_at']}\n\n"
                f"Data: {json.dumps(item['data'], indent=2, ensure_ascii=False)}"
            )

            await telegram_channel.send_message(self.admin_chat_id, message)
        except Exception:
            # Don't fail if alert fails
            pass

    async def dequeue(self, timeout: int = 0) -> dict[str, Any] | None:
        """Dequeue item from fallback queue.

        Args:
            timeout: Blocking timeout in seconds (0 = non-blocking)

        Returns:
            Queue item or None
        """
        item_str = await redis_storage.rpop(self.QUEUE_KEY, timeout=timeout)
        if item_str is None:
            return None

        try:
            return json.loads(item_str)
        except json.JSONDecodeError:
            return None

    async def size(self) -> int:
        """Get queue size.

        Returns:
            Number of items in queue
        """
        try:
            return await redis_storage.llen(self.QUEUE_KEY)
        except Exception:
            return 0


@lru_cache()
def get_fallback() -> ImpulseFallback:
    """Get fallback instance (lazy init)."""
    return ImpulseFallback()

