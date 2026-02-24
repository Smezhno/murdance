"""FastAPI application entry point.

Per CONTRACT §22: Webhook endpoints and health check.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse

from app.channels.dedup import is_duplicate
from app.channels.filters import get_non_text_reply, should_process
from app.channels.telegram import get_telegram_channel
from app.config import get_settings
from app.queue.outbound import enqueue_message
from app.storage.postgres import postgres_storage


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: connect/disconnect storage."""
    # Startup: run migrations, validate KB, start cleanup scheduler
    await postgres_storage.connect()

    from app.knowledge.base import load_knowledge_base

    load_knowledge_base()  # Raises if invalid - app must not start

    from app.core.cleanup import start_scheduler

    scheduler = start_scheduler()

    yield

    # Shutdown
    scheduler.shutdown(wait=False)
    await postgres_storage.disconnect()


app = FastAPI(
    title="DanceBot",
    description="AI chatbot backend for dance studio",
    lifespan=lifespan,
)


@app.post("/webhook/telegram")
async def telegram_webhook(request: Request) -> Response:
    """Telegram webhook endpoint (CONTRACT §22).

    Per CONTRACT §8, §19:
    - Verify signature
    - Deduplicate messages
    - Filter non-text messages
    - Process text messages
    """
    telegram_channel = get_telegram_channel()

    # Verify signature (CONTRACT §19)
    if not telegram_channel.verify_signature(request):
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)

    try:
        # Parse webhook
        message = await telegram_channel.parse_webhook(request)

        # Deduplicate (CONTRACT §8)
        if await is_duplicate(message):
            return Response(status_code=status.HTTP_200_OK)  # Accept but don't process

        # Log inbound message (CONTRACT §17)
        await postgres_storage.log_message(
            trace_id=message.trace_id,
            channel=message.channel,
            chat_id=message.chat_id,
            message_id=message.message_id,
            timestamp=message.timestamp,
            text=message.text,
            message_type=message.message_type,
            direction="inbound",
            sender_phone=message.sender_phone,
            sender_name=message.sender_name,
        )

        # Filter non-text messages (CONTRACT §8)
        if not should_process(message):
            # Enqueue friendly reply via outbound_queue (CONTRACT §9)
            reply_text = get_non_text_reply(message)
            await enqueue_message(
                chat_id=message.chat_id,
                channel=message.channel,
                text=reply_text,
                trace_id=message.trace_id,
            )
            return Response(status_code=status.HTTP_200_OK)

        # Typing indicator: sent directly — it's a real-time signal that
        # would be stale by the time the worker processes it from the queue.
        await telegram_channel.send_typing(message.chat_id)

        # Process message through conversation engine (RFC-003)
        from app.core.engine import get_conversation_engine

        engine = get_conversation_engine()
        response_text = await engine.handle_message(message, message.trace_id)

        # Enqueue response via outbound_queue (CONTRACT §9)
        queue_id = await enqueue_message(
            chat_id=message.chat_id,
            channel=message.channel,
            text=response_text,
            trace_id=message.trace_id,
        )

        # Log outbound message (CONTRACT §17)
        await postgres_storage.log_message(
            trace_id=message.trace_id,
            channel=message.channel,
            chat_id=message.chat_id,
            message_id=str(queue_id),
            timestamp=message.timestamp,
            text=response_text,
            message_type="text",
            direction="outbound",
        )

        return Response(status_code=status.HTTP_200_OK)

    except ValueError as e:
        # Invalid webhook data
        return Response(status_code=status.HTTP_400_BAD_REQUEST)
    except Exception as e:
        # Log error to Postgres (CONTRACT §17)
        import traceback

        await postgres_storage.log_error(
            error_type=type(e).__name__,
            error_message=str(e),
            stack_trace=traceback.format_exc(),
        )
        return Response(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


@app.get("/health")
async def health_check() -> JSONResponse:
    """Health check endpoint (CONTRACT §22).

    Returns: {status, postgres, crm, pool_stats}
    """
    # Check Postgres
    postgres_healthy = False
    try:
        postgres_healthy = await postgres_storage.health_check()
    except Exception:
        pass

    # Check CRM
    crm_healthy = False
    try:
        from app.integrations.impulse import get_impulse_adapter

        impulse = get_impulse_adapter()
        crm_healthy = await impulse.health_check()
    except Exception:
        pass

    pool = {}
    try:
        pool = postgres_storage.pool_stats()
    except Exception:
        pass

    overall_status = "healthy" if (postgres_healthy and crm_healthy) else "degraded"

    return JSONResponse(
        status_code=status.HTTP_200_OK if overall_status == "healthy" else status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "status": overall_status,
            "postgres": "healthy" if postgres_healthy else "unhealthy",
            "crm": "healthy" if crm_healthy else "unhealthy",
            "pool": pool,
        },
    )


@app.get("/")
async def root() -> dict[str, str]:
    """Root endpoint."""
    return {"message": "DanceBot API", "version": "0.1.0"}


@app.post("/debug")
async def debug_command(request: Request) -> JSONResponse:
    """Debug endpoint for testing (CONTRACT §22).

    Accepts Telegram webhook format, processes through booking flow.
    Guarded by TEST_MODE setting.
    """
    settings = get_settings()
    if not settings.test_mode:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"error": "Not found"},
        )

    telegram_channel = get_telegram_channel()

    try:
        # Parse webhook
        message = await telegram_channel.parse_webhook(request)

        # Process through conversation engine (RFC-003)
        from app.core.engine import get_conversation_engine

        engine = get_conversation_engine()
        response_text = await engine.handle_message(message, message.trace_id)

        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "trace_id": str(message.trace_id),
                "response": response_text,
            },
        )

    except Exception as e:
        import traceback

        await postgres_storage.log_error(
            error_type=type(e).__name__,
            error_message=str(e),
            stack_trace=traceback.format_exc(),
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": str(e)},
        )
