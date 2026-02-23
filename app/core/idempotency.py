"""Idempotency guard for bookings.

Per CONTRACT §10: fingerprint = sha256(phone + schedule_id).
                  INSERT into idempotency_locks BEFORE CRM call.
                  On duplicate: "You're already booked ✅". One booking only, even under retries.

Key difference from the Redis version:
    - Expired locks STAY in the table for 24h audit trail.
    - The 10-minute active window is enforced by checking created_at in Python,
      NOT by a TTL on the row. Cleanup is done by the periodic job.
    - UniqueViolationError replaces SETNX — atomicity is guaranteed by the PK.

Per RFC-002 §3.2.2 — PostgreSQL pattern for idempotency.
"""

import hashlib
import logging

import asyncpg

from app.storage.postgres import postgres_storage as db

logger = logging.getLogger(__name__)

# Active window for duplicate detection (CONTRACT §10).
# Rows older than this are treated as expired and allow a retry.
_LOCK_ACTIVE_WINDOW = "10 minutes"


def compute_fingerprint(phone: str, schedule_id: int | str) -> str:
    """Return SHA-256 hex digest of phone+schedule_id (CONTRACT §10).

    The fingerprint is the PRIMARY KEY of idempotency_locks, so collisions
    would block a booking — use the full 64-char hex string.
    """
    return hashlib.sha256(f"{phone}{schedule_id}".encode()).hexdigest()


async def acquire_booking_lock(
    phone: str,
    schedule_id: int | str,
    channel: str = "",
    chat_id: str = "",
) -> tuple[bool, str]:
    """Atomically acquire an idempotency lock BEFORE calling CRM (CONTRACT §10).

    Algorithm (RFC-002 §3.2.2):
      1. INSERT fingerprint → if success: new booking, return (True, "").
      2. UniqueViolationError → fingerprint already exists:
         a. If created_at > now() - 10min: still active → duplicate, return (False, msg).
         b. If created_at <= now() - 10min: lock expired → allow retry, return (True, "").

    Args:
        phone:       Client phone number (used to compute fingerprint).
        schedule_id: CRM schedule ID (used to compute fingerprint).
        channel:     Channel name — stored for audit context (optional).
        chat_id:     Chat ID — stored for audit context (optional).

    Returns:
        (True, "")                          — new booking, proceed to CRM.
        (False, "Вы уже записаны …")        — active duplicate, abort.
    """
    fingerprint = compute_fingerprint(phone, schedule_id)

    try:
        await db.execute(
            """
            INSERT INTO idempotency_locks
                (fingerprint, channel, chat_id, client_phone, schedule_id)
            VALUES ($1, $2, $3, $4, $5)
            """,
            fingerprint,
            channel,
            chat_id,
            phone,
            str(schedule_id),
        )
        return True, ""

    except asyncpg.UniqueViolationError:
        # Row exists. Check whether the lock is still within the active window.
        row = await db.fetchrow(
            f"""
            SELECT created_at FROM idempotency_locks
            WHERE fingerprint = $1
              AND created_at > NOW() - INTERVAL '{_LOCK_ACTIVE_WINDOW}'
            """,
            fingerprint,
        )

        if row is not None:
            # Lock is active — this is a genuine duplicate booking attempt.
            logger.info(
                "idempotency: duplicate blocked fingerprint=%.8s phone=%s schedule_id=%s",
                fingerprint, phone, schedule_id,
            )
            return False, "Вы уже записаны на это занятие ✅"

        # Lock exists but is older than 10 min — treat as expired, allow retry.
        # We do NOT delete the old row here; the periodic cleanup job handles it.
        # Overwrite with a fresh row so the new attempt starts its own 10-min window.
        logger.info(
            "idempotency: expired lock replaced fingerprint=%.8s phone=%s schedule_id=%s",
            fingerprint, phone, schedule_id,
        )
        await db.execute(
            """
            UPDATE idempotency_locks
            SET channel = $2, chat_id = $3, created_at = NOW()
            WHERE fingerprint = $1
            """,
            fingerprint,
            channel,
            chat_id,
        )
        return True, ""


async def release_booking_lock(phone: str, schedule_id: int | str) -> None:
    """Delete the idempotency lock (called on CRM error to allow an immediate retry).

    Under normal success the lock stays in the table for the full 24h audit window
    and is cleaned by the periodic job. This function is only used when the CRM call
    itself failed and the booking was NOT created — so we must unblock the client.
    """
    fingerprint = compute_fingerprint(phone, schedule_id)
    await db.execute(
        "DELETE FROM idempotency_locks WHERE fingerprint = $1",
        fingerprint,
    )
    logger.debug(
        "idempotency: lock released fingerprint=%.8s phone=%s schedule_id=%s",
        fingerprint, phone, schedule_id,
    )
