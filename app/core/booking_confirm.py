"""Booking confirmation, receipt generation, and CRM booking creation.

Extracted from booking_flow.py to keep that file as a thin router.
"""

import logging
from typing import Any
from uuid import UUID

from app.core.conversation import transition_state
from app.core.idempotency import acquire_booking_lock, release_booking_lock
from app.core.response_generator import generate_response
from app.models import ConversationState
from app.storage.postgres import postgres_storage

logger = logging.getLogger(__name__)


def generate_confirmation_summary(session: Any) -> str:
    """Return deterministic confirmation summary (no LLM — data must be exact).

    Includes teacher if set, and booking branch if known from KB.
    """
    slots = session.slots
    if slots.datetime_resolved:
        datetime_str = slots.datetime_resolved.strftime("%d.%m.%Y %H:%M")
    elif slots.datetime_raw:
        datetime_str = slots.datetime_raw
    else:
        datetime_str = "не указано"

    teacher_line = f"\nПреподаватель: {slots.teacher}" if getattr(slots, "teacher", None) else ""

    return (
        f"Подтвердите запись:\n\n"
        f"Направление: {slots.group or 'не указано'}"
        f"{teacher_line}\n"
        f"Дата и время: {datetime_str}\n"
        f"Имя: {slots.client_name or 'не указано'}\n"
        f"Телефон: {slots.client_phone or 'не указано'}\n\n"
        f"Подтверждаешь? (да/нет)"
    )


def generate_receipt(
    reservation: Any,
    client: Any,
    session: Any,
    kb: Any,
) -> str:
    """Generate booking receipt (CONTRACT §6: ≤300 chars structured block)."""
    slots = session.slots
    group = slots.group or "не указано"
    if slots.datetime_resolved:
        datetime_str = slots.datetime_resolved.strftime("%d.%m.%Y %H:%M")
    elif slots.datetime_raw:
        datetime_str = slots.datetime_raw
    else:
        datetime_str = "не указано"

    # Branch address: prefer branch-specific address, fall back to generic studio address
    branch_address = (
        kb.get_branch_address(slots.branch)
        if getattr(slots, "branch", None)
        else None
    ) or kb.studio.address

    # Dress code: append only when found
    dress_code = kb.get_dress_code(slots.group) if getattr(slots, "group", None) else None

    reservation_line = f"\nНомер записи: {reservation.id}" if getattr(reservation, "id", None) else ""
    dress_code_line = f"\nС собой: {dress_code}" if dress_code else ""

    receipt = (
        f"✅ Запись подтверждена!\n\n"
        f"Направление: {group}\n"
        f"Дата и время: {datetime_str}\n"
        f"Имя: {client.name}\n"
        f"Телефон: {client.phone_str}\n"
        f"Адрес: {branch_address}"
        f"{reservation_line}"
        f"{dress_code_line}"
    )

    # Truncate if over 300 chars
    if len(receipt) > 300:
        receipt = receipt[:297] + "..."

    return receipt


async def confirm_booking(
    session: Any,
    trace_id: UUID,
    impulse_adapter: Any,
    kb: Any,
) -> str:
    """Create booking in CRM after user confirmation (CONTRACT §6, §7, §10).

    Single try/except/finally block — logs once, releases lock on failure.
    """
    if not session.slots.group or not session.slots.datetime_resolved:
        return "Не все данные заполнены. Укажи направление и дату."

    await transition_state(session, ConversationState.BOOKING_IN_PROGRESS)

    phone = session.slots.client_phone
    schedule_id: Any = session.slots.schedule_id
    lock_acquired = False
    success = False
    client = None

    try:
        # Phase 1 — find or create client
        client = await impulse_adapter.find_client(phone)
        if not client:
            client = await impulse_adapter.create_client(
                name=session.slots.client_name or "Клиент",
                phone=phone,
                trace_id=trace_id,
            )

        # Phase 2 — resolve schedule_id
        if not schedule_id:
            target_dt = session.slots.datetime_resolved
            target_weekday = target_dt.weekday()
            target_minutes = target_dt.hour * 60 + target_dt.minute
            group_lower = (session.slots.group or "").lower()
            teacher_lower = (getattr(session.slots, "teacher", None) or "").lower()

            schedules = await impulse_adapter.get_schedule()
            for sch in schedules:
                if sch.day != target_weekday:
                    continue
                if sch.minutes_begin is None or abs(sch.minutes_begin - target_minutes) > 30:
                    continue
                if group_lower and group_lower not in sch.style_name.lower():
                    continue
                if teacher_lower and (not sch.teacher_name or teacher_lower not in sch.teacher_name.lower()):
                    continue
                schedule_id = sch.id
                break

            if not schedule_id:
                logger.warning(
                    "No schedule found weekday=%s min=%s group=%s trace=%s",
                    target_weekday, target_minutes, session.slots.group, trace_id,
                )
                return await generate_response("error_crm", {}, trace_id)

        # Phase 3 — idempotency lock (CONTRACT §10)
        is_new, idempotency_msg = await acquire_booking_lock(phone, schedule_id)
        if not is_new:
            await transition_state(session, ConversationState.IDLE)
            return idempotency_msg
        lock_acquired = True

        # Phase 4 — create booking in CRM
        reservation = await impulse_adapter.create_booking(
            client_id=client.id,
            schedule_id=schedule_id,
            booking_date=session.slots.datetime_resolved,
            trace_id=trace_id,
        )
        success = True
        await transition_state(session, ConversationState.BOOKING_DONE)
        return generate_receipt(reservation, client, session, kb)

    except RuntimeError as e:
        return str(e)
    except Exception:
        logger.exception("confirm_booking unexpected error trace=%s", trace_id)
        return await generate_response("error_crm", {}, trace_id)
    finally:
        if lock_acquired and not success:
            await release_booking_lock(phone, schedule_id)
        await postgres_storage.log_booking_attempt(
            trace_id=trace_id,
            channel=session.channel,
            chat_id=session.chat_id,
            success=success,
            group_id=session.slots.group,
            schedule_id=str(schedule_id) if schedule_id else None,
            datetime_=session.slots.datetime_resolved,
            client_name=session.slots.client_name,
            client_phone=phone,
            error_message=None if success else "booking failed",
        )
        if not success and session.state not in (
            ConversationState.IDLE, ConversationState.BOOKING_IN_PROGRESS
        ):
            await transition_state(session, ConversationState.IDLE)
