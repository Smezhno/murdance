"""Fallback queue for CRM errors.

Per CONTRACT §5: PostgreSQL outbound_queue fallback + TG admin alert.

Replaces former Redis LPUSH/RPOP on impulse:fallback:queue.
Items are stored in outbound_queue with priority=10 (low) and type='crm_fallback'.
The worker picks them up and retries CRM operations.
"""

import json
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any
from uuid import UUID, uuid4

from app.config import get_settings
from app.queue.outbound import enqueue_message
from app.storage.postgres import postgres_storage as db


class ImpulseFallback:
    """Fallback queue for CRM errors (CONTRACT §5)."""

    PRIORITY = 10  # Low priority — retry, not real-time

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

        item_id = str(uuid4())
        created_at = datetime.now(timezone.utc).isoformat()

        payload = {
            "id": item_id,
            "trace_id": trace_id,
            "action": action,
            "data": data,
            "error": error,
            "created_at": created_at,
        }

        # text: human-readable summary for the worker / admin audit log.
        # chat_id: admin chat ID — the worker sends the retry result there.
        # trace_id column: stored as UUID for observability joins.
        text = f"[crm_fallback] action={action} error={error[:200]}"

        await db.execute(
            """
            INSERT INTO outbound_queue
                (channel, chat_id, text, payload, trace_id, priority)
            VALUES ('crm_fallback', $1, $2, $3, $4::uuid, $5)
            """,
            str(self.admin_chat_id),
            text,
            payload,
            trace_id,
            self.PRIORITY,
        )

        await self._send_admin_alert(payload)

    async def _send_admin_alert(self, item: dict[str, Any]) -> None:
        """Enqueue admin alert via outbound_queue (priority=1, admin tier).

        Goes through the same queue and HTTP client as all other messages,
        so VPN proxy configuration (RFC-002 §4) applies automatically.
        Priority 1 puts it ahead of crm_fallback rows (10) but behind
        interactive user replies (0).
        """
        try:
            text = (
                f"⚠️ CRM Fallback Queue\n\n"
                f"Action: {item['action']}\n"
                f"Error: {item['error']}\n"
                f"Trace ID: {item['trace_id']}\n"
                f"Created: {item['created_at']}\n\n"
                f"Data: {json.dumps(item['data'], indent=2, ensure_ascii=False)}"
            )
            await enqueue_message(
                chat_id=str(self.admin_chat_id),
                channel="telegram",
                text=text,
                trace_id=UUID(item["trace_id"]),
                priority=1,
            )
        except Exception:
            pass

    async def dequeue(self, timeout: int = 0) -> dict[str, Any] | None:
        """Dequeue next pending item from fallback queue.

        Args:
            timeout: Ignored (kept for API compatibility; PG is non-blocking)

        Returns:
            Queue item payload or None
        """
        row = await db.fetchrow(
            """
            DELETE FROM outbound_queue
            WHERE id = (
                SELECT id FROM outbound_queue
                WHERE channel = 'crm_fallback' AND status = 'pending'
                ORDER BY priority ASC, id ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING payload
            """
        )
        if row is None:
            return None
        return dict(row["payload"])

    async def size(self) -> int:
        """Get queue size.

        Returns:
            Number of pending items in queue
        """
        try:
            result = await db.fetchval(
                "SELECT COUNT(*) FROM outbound_queue WHERE channel = 'crm_fallback' AND status = 'pending'"
            )
            return int(result or 0)
        except Exception:
            return 0


@lru_cache()
def get_fallback() -> ImpulseFallback:
    """Get fallback instance (lazy init)."""
    return ImpulseFallback()
