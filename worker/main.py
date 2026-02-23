"""Worker process — CONTRACT §9.

Responsibilities:
  - Poll outbound_queue for pending messages (FOR UPDATE SKIP LOCKED).
  - Send via direct httpx calls to api.telegram.org (no VPN proxy yet —
    that is a separate task per RFC-002 §4).
  - Mark sent / retry / DLQ on outcome.
  - LISTEN on 'outbound_new' for zero-latency wakeup (RFC-002 §3.3).
  - Alert admin when DLQ count > 10 (CONTRACT §9).

Retry policy (CONTRACT §9): 0s → 5s → 30s → DLQ (status='failed').
  attempt 0 → fail → retry immediately (next_attempt_at = now)
  attempt 1 → fail → retry after 5s
  attempt 2 → fail → status='failed' (DLQ)

Rate limit: Telegram 30 msg/s (CONTRACT §9).
  Worker sends at most BATCH_SIZE=10 per cycle; each send is sequential
  within the batch to stay well under the limit.

Poll order: priority ASC, id ASC (0 = highest priority, FIFO within tier).
  Per migration DDL comment: 0=interactive reply, 1=reminder, 2=admin.
"""

import asyncio
import logging
import signal
from typing import Any
from uuid import uuid4

import asyncpg
import httpx

from app.config import get_settings
from app.queue.outbound import (
    MAX_RETRIES,
    enqueue_message,
    get_dlq_count,
    mark_failed,
    mark_retry,
    mark_sent,
)
from app.storage.postgres import postgres_storage

logger = logging.getLogger(__name__)

BATCH_SIZE = 10
POLL_INTERVAL_SECONDS = 5
DLQ_ALERT_THRESHOLD = 10

# Telegram Bot API base URL. VPN proxy will be wired here in a future task.
_TG_API_BASE = "https://api.telegram.org/bot{token}"

# Telegram message length limit.
_TG_MAX_TEXT = 4096


# ---------------------------------------------------------------------------
# Direct Telegram sender (httpx, no aiogram dependency in worker)
# ---------------------------------------------------------------------------

class _TelegramSender:
    """Minimal Telegram Bot API client using httpx.AsyncClient.

    Intentionally does NOT use the app's TelegramChannel / aiogram adapter.
    The worker is an independent process; it owns its own HTTP client so that
    VPN proxy configuration (RFC-002 §4) can be wired here without touching
    the inbound webhook path.
    """

    def __init__(self, bot_token: str) -> None:
        self._base = _TG_API_BASE.format(token=bot_token)
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0))

    async def stop(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def send_text(self, chat_id: str, text: str) -> None:
        """Send a plain-text message. Raises on non-2xx response."""
        assert self._client is not None, "call start() first"
        if len(text) > _TG_MAX_TEXT:
            text = text[: _TG_MAX_TEXT - 3] + "..."
        resp = await self._client.post(
            f"{self._base}/sendMessage",
            json={"chat_id": chat_id, "text": text},
        )
        resp.raise_for_status()

    async def send_buttons(
        self, chat_id: str, text: str, buttons: list[dict[str, Any]]
    ) -> None:
        """Send a message with an inline keyboard. Raises on non-2xx response."""
        assert self._client is not None, "call start() first"
        if len(text) > _TG_MAX_TEXT:
            text = text[: _TG_MAX_TEXT - 3] + "..."
        keyboard = {
            "inline_keyboard": [
                [{"text": b["text"], "callback_data": b.get("callback_data", "")}]
                for b in buttons
            ]
        }
        resp = await self._client.post(
            f"{self._base}/sendMessage",
            json={"chat_id": chat_id, "text": text, "reply_markup": keyboard},
        )
        resp.raise_for_status()


# ---------------------------------------------------------------------------
# Poll / send loop
# ---------------------------------------------------------------------------

async def _poll_batch(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    """Atomically claim a batch of due pending messages.

    Sets status='sending' so concurrent workers (future scale-out) cannot
    double-process the same row. FOR UPDATE SKIP LOCKED ensures no blocking.

    Order: priority ASC (0 = highest), id ASC (FIFO within a tier).
    """
    rows = await conn.fetch(
        """
        UPDATE outbound_queue
           SET status     = 'sending',
               updated_at = NOW()
         WHERE id IN (
             SELECT id FROM outbound_queue
              WHERE status = 'pending'
                AND next_attempt_at <= NOW()
              ORDER BY priority ASC, id ASC
              FOR UPDATE SKIP LOCKED
              LIMIT $1
         )
         RETURNING id, channel, chat_id, text, payload, trace_id,
                   attempts, priority
        """,
        BATCH_SIZE,
    )
    return [dict(r) for r in rows]


async def _send_one(
    msg: dict[str, Any],
    sender: _TelegramSender,
) -> None:
    """Attempt delivery of one outbound message; update status on outcome."""
    msg_id: int = msg["id"]
    channel: str = msg["channel"]
    chat_id: str = msg["chat_id"]
    text: str = msg["text"]
    payload: dict = msg["payload"] or {}

    try:
        if channel == "telegram":
            msg_type = payload.get("type", "text")
            if msg_type == "buttons":
                await sender.send_buttons(chat_id, text, payload.get("buttons", []))
            else:
                await sender.send_text(chat_id, text)
            await mark_sent(msg_id)
            logger.info("worker: sent id=%d chat_id=%s", msg_id, chat_id)

        elif channel == "crm_fallback":
            # crm_fallback rows are consumed by ImpulseFallback.dequeue(),
            # not delivered as Telegram messages. Release the lock.
            await mark_retry(msg_id)

        else:
            await mark_failed(msg_id, f"unknown channel: {channel!r}")
            logger.warning("worker: unknown channel id=%d channel=%s", msg_id, channel)

    except httpx.HTTPStatusError as exc:
        error = f"HTTP {exc.response.status_code}: {exc.response.text[:500]}"
        logger.warning("worker: HTTP error id=%d: %s", msg_id, error)
        await mark_failed(msg_id, error)

    except Exception as exc:
        error = str(exc)[:2000]
        logger.exception("worker: send failed id=%d channel=%s", msg_id, channel)
        await mark_failed(msg_id, error)


async def _check_dlq_alert() -> None:
    """Enqueue a one-shot admin alert when DLQ count exceeds threshold.

    CONTRACT §9: DLQ > 10 → alert admin.
    Goes through outbound_queue (priority=1) so VPN proxy applies
    automatically — same path as all other messages.
    """
    global _dlq_alerted  # noqa: PLW0603
    try:
        count = await get_dlq_count()
        if count > DLQ_ALERT_THRESHOLD and not _dlq_alerted:
            settings = get_settings()
            await enqueue_message(
                chat_id=str(settings.admin_telegram_chat_id),
                channel="telegram",
                text=(
                    f"⚠️ DLQ alert: {count} messages failed all {MAX_RETRIES} retries.\n"
                    f"Check: SELECT * FROM outbound_queue WHERE status='failed';"
                ),
                trace_id=uuid4(),
                priority=1,
            )
            _dlq_alerted = True
            logger.warning("worker: DLQ alert enqueued count=%d", count)
        elif count <= DLQ_ALERT_THRESHOLD:
            _dlq_alerted = False  # Reset so the next spike triggers a fresh alert
    except Exception:
        logger.exception("worker: DLQ alert check failed")


_dlq_alerted: bool = False


async def _run_poll_cycle(pool: asyncpg.Pool, sender: _TelegramSender) -> int:
    """Claim a batch, send each message sequentially, return count processed.

    Sequential (not concurrent) within a batch to respect Telegram rate limits
    (30 msg/s). At BATCH_SIZE=10 and ~100ms per send we're at ~100 msg/s max,
    but real-world latency keeps us well under the limit.
    """
    async with pool.acquire() as conn:
        batch = await _poll_batch(conn)

    if not batch:
        return 0

    for msg in batch:
        await _send_one(msg, sender)

    await _check_dlq_alert()
    return len(batch)


# ---------------------------------------------------------------------------
# Main loop: LISTEN/NOTIFY + periodic poll fallback
# ---------------------------------------------------------------------------

async def _worker_loop(
    pool: asyncpg.Pool,
    sender: _TelegramSender,
    stop_event: asyncio.Event,
) -> None:
    """Main worker loop (RFC-002 §3.3).

    LISTEN on 'outbound_new' wakes the loop immediately when the app inserts
    a new row. The 5-second fallback poll handles:
      - Retry rows whose next_attempt_at has elapsed.
      - Any NOTIFY that was missed (e.g. during a transient disconnect).

    A dedicated connection is used for LISTEN because asyncpg does not allow
    mixing LISTEN with pool.acquire() on the same connection.
    """
    settings = get_settings()
    listen_conn: asyncpg.Connection = await asyncpg.connect(settings.postgres_url)

    notify_event = asyncio.Event()

    def _on_notify(
        _conn: asyncpg.Connection,
        _pid: int,
        _channel: str,
        _payload: str,
    ) -> None:
        notify_event.set()

    await listen_conn.add_listener("outbound_new", _on_notify)
    logger.info("worker: LISTEN outbound_new — ready")

    try:
        while not stop_event.is_set():
            processed = await _run_poll_cycle(pool, sender)

            if processed == BATCH_SIZE:
                # Full batch returned — there may be more rows waiting.
                # Skip the sleep and poll again immediately.
                continue

            # Wait for NOTIFY or the fallback timeout, whichever comes first.
            try:
                await asyncio.wait_for(
                    notify_event.wait(),
                    timeout=POLL_INTERVAL_SECONDS,
                )
                notify_event.clear()
            except asyncio.TimeoutError:
                pass  # Normal — periodic poll

    finally:
        await listen_conn.remove_listener("outbound_new", _on_notify)
        await listen_conn.close()
        logger.info("worker: LISTEN connection closed")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    """Worker entry point (CONTRACT §9)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger.info("worker: starting")

    settings = get_settings()

    # Connect to PostgreSQL (same pool settings as app: min=2, max=10).
    await postgres_storage.connect()
    pool: asyncpg.Pool = postgres_storage.pool  # type: ignore[assignment]

    # Build the Telegram sender (direct httpx, no VPN proxy yet).
    sender = _TelegramSender(settings.telegram_bot_token)
    await sender.start()

    stop_event = asyncio.Event()

    def _handle_signal(sig: int) -> None:
        logger.info("worker: signal %d received — shutting down", sig)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal, sig)

    try:
        await _worker_loop(pool, sender, stop_event)
    finally:
        # On graceful shutdown: release any rows stuck in 'sending' back to
        # 'pending' so they are retried on the next worker start.
        logger.info("worker: releasing in-flight 'sending' rows back to 'pending'")
        try:
            async with pool.acquire() as conn:
                released = await conn.execute(
                    "UPDATE outbound_queue SET status='pending', updated_at=NOW() "
                    "WHERE status='sending'"
                )
            logger.info("worker: released %s", released)
        except Exception:
            logger.exception("worker: failed to release sending rows")

        await sender.stop()
        await postgres_storage.disconnect()
        logger.info("worker: stopped")


if __name__ == "__main__":
    asyncio.run(main())
