"""Idempotency guard for bookings.

Per CONTRACT §10: fingerprint = sha256(phone + schedule_id), Redis SETNX before CRM call.
On duplicate: "You're already booked ✅". One booking only, even under retries.
"""

import hashlib

from app.storage.redis import redis_storage


def compute_fingerprint(phone: str, schedule_id: int | str) -> str:
    """Compute booking fingerprint (CONTRACT §10).

    Args:
        phone: Client phone number
        schedule_id: Schedule ID

    Returns:
        SHA256 hash as hex string
    """
    fingerprint_string = f"{phone}{schedule_id}"
    return hashlib.sha256(fingerprint_string.encode()).hexdigest()


def get_idempotency_key(fingerprint: str) -> str:
    """Get Redis key for idempotency lock.

    Args:
        fingerprint: SHA256 fingerprint

    Returns:
        Redis key string
    """
    return f"idempotency:{fingerprint}"


async def acquire_booking_lock(phone: str, schedule_id: int | str) -> tuple[bool, str]:
    """Acquire idempotency lock for booking (CONTRACT §10).

    Sets Redis lock BEFORE CRM call. Returns True if lock acquired (new booking),
    False if duplicate (already booked).

    Args:
        phone: Client phone number
        schedule_id: Schedule ID

    Returns:
        Tuple of (is_new_booking, message)
        is_new_booking: True if lock acquired, False if duplicate
        message: User message ("already booked" if duplicate)
    """
    fingerprint = compute_fingerprint(phone, schedule_id)
    key = get_idempotency_key(fingerprint)

    # SETNX with 10min TTL (CONTRACT §10)
    # Returns True if key was created (new booking), False if already exists (duplicate)
    is_new = await redis_storage.setnx(key, "1", ex=600)  # 10 min TTL

    if is_new:
        return True, ""
    else:
        return False, "Вы уже записаны на это занятие ✅"


async def release_booking_lock(phone: str, schedule_id: int | str) -> None:
    """Release idempotency lock (for cleanup on error).

    Args:
        phone: Client phone number
        schedule_id: Schedule ID
    """
    fingerprint = compute_fingerprint(phone, schedule_id)
    key = get_idempotency_key(fingerprint)
    await redis_storage.delete(key)

