"""Inbound message deduplication.

Per CONTRACT §8: SETNX seen:{channel}:{message_id} TTL 5min.
If exists → drop (duplicate message).
"""

from app.models import UnifiedMessage
from app.storage.redis import redis_storage


async def is_duplicate(message: UnifiedMessage) -> bool:
    """Check if message is duplicate (CONTRACT §8).

    Uses Redis SETNX with 5min TTL to detect duplicate webhooks.

    Args:
        message: UnifiedMessage to check

    Returns:
        True if duplicate, False if new message
    """
    key = f"seen:{message.channel}:{message.message_id}"
    # SETNX: returns True if key was created (new), False if already exists (duplicate)
    is_new = await redis_storage.setnx(key, "1", ex=300)  # 5 min TTL
    return not is_new  # True = duplicate
