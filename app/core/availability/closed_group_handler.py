"""Closed group handler — availability check before booking (RFC-005 §7.3).

Extracted from engine.py to keep it under 300 lines.
"""

import logging
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from app.core.availability.protocol import AvailabilityStatus, GroupAvailability
from app.integrations.impulse.models import impulse_day_to_weekday
from app.core.conversation import update_slots
from app.models import SlotValues

logger = logging.getLogger(__name__)


async def resolve_schedule_id_and_date(
    slots: SlotValues,
    impulse_adapter: Any,
) -> tuple[int | None, date | None]:
    """Resolve schedule_id and target_date from slots (mirrors confirm_booking Phase 2)."""
    sid = slots.schedule_id
    if sid is not None:
        try:
            return int(sid), slots.datetime_resolved.date() if slots.datetime_resolved else None
        except (TypeError, ValueError):
            pass
    if not slots.datetime_resolved:
        return None, None
    target_dt = slots.datetime_resolved
    target_weekday = target_dt.weekday()
    target_minutes = target_dt.hour * 60 + target_dt.minute
    group_lower = (slots.group or "").lower()
    teacher_lower = (getattr(slots, "teacher", None) or "").lower()
    schedules = await impulse_adapter.get_schedule()
    for sch in schedules:
        if sch.day is None or impulse_day_to_weekday(sch.day) != target_weekday:
            continue
        if sch.minutes_begin is None or abs(sch.minutes_begin - target_minutes) > 30:
            continue
        if group_lower and group_lower not in (sch.style_name or "").lower():
            continue
        if teacher_lower and (not sch.teacher_name or teacher_lower not in sch.teacher_name.lower()):
            continue
        return sch.id, target_dt.date()
    return None, None


async def check_closed_before_booking(
    availability_provider: Any,
    slots: SlotValues,
    impulse_adapter: Any,
) -> GroupAvailability | None:
    """If group is CLOSED/HOLIDAY return GroupAvailability, else None.

    Returns None if no availability_provider or no schedule_id."""
    if not availability_provider:
        return None
    schedule_id, target_date = await resolve_schedule_id_and_date(slots, impulse_adapter)
    if not schedule_id or not target_date:
        return None
    try:
        avail = await availability_provider.get_availability(schedule_id, target_date)
        if avail.status in (AvailabilityStatus.CLOSED, AvailabilityStatus.HOLIDAY):
            return avail
    except Exception as e:
        logger.warning("availability_check_failed: %s", e)
    return None


async def handle_closed_group(
    session: Any,
    slots: SlotValues,
    avail: GroupAvailability,
    availability_provider: Any,
    impulse_adapter: Any,
) -> str:
    """Handle closed group: next date → alternatives → escalation (RFC-005 §7.3)."""
    try:
        return await _handle_closed_group_impl(
            session, slots, avail, availability_provider, impulse_adapter
        )
    except Exception as e:
        logger.warning("handle_closed_group failed: %s", e)
        await update_slots(session, confirmed=False)
        return "Уточню у администратора и напишу тебе. Подожди немного!"


async def _handle_closed_group_impl(
    session: Any,
    slots: SlotValues,
    avail: GroupAvailability,
    availability_provider: Any,
    impulse_adapter: Any,
) -> str:
    schedule_id = avail.schedule_id
    target_date = avail.date
    style_id = slots.style_id
    branch_id = slots.branch_id
    teacher_id = slots.teacher_id
    if style_id is not None:
        try:
            style_id = int(style_id)
        except (TypeError, ValueError):
            style_id = None
    if branch_id is not None:
        try:
            branch_id = int(branch_id)
        except (TypeError, ValueError):
            branch_id = None
    if teacher_id is not None:
        try:
            teacher_id = int(teacher_id)
        except (TypeError, ValueError):
            teacher_id = None

    closed_date_str = target_date.strftime("%d.%m.%Y")

    try:
        next_avail = await availability_provider.find_next_open(schedule_id, target_date)
        if next_avail is not None:
            next_date_str = next_avail.date.strftime("%d.%m.%Y")
            schedules = await impulse_adapter.get_schedule()
            sch = next((s for s in schedules if s.id == schedule_id), None)
            hour, minute = (19, 30)
            if sch and sch.minutes_begin is not None:
                hour, minute = divmod(sch.minutes_begin, 60)
            tz = ZoneInfo("Asia/Vladivostok")
            new_dt = datetime.combine(next_avail.date, time(hour, minute), tzinfo=tz)
            await update_slots(
                session,
                confirmed=False,
                datetime_resolved=new_dt,
                schedule_id=str(schedule_id),
            )
            return f"На {closed_date_str} запись в эту группу закрыта. Ближайшая открытая дата — {next_date_str}. Записать?"
    except Exception as e:
        logger.warning("find_next_open failed: %s", e)

    if style_id is None:
        schedules = await impulse_adapter.get_schedule()
        sch = next((s for s in schedules if s.id == schedule_id), None)
        if sch and sch.group and isinstance(sch.group.get("style"), dict):
            try:
                style_id = int((sch.group.get("style") or {}).get("id"))
            except (TypeError, ValueError):
                style_id = None
    if style_id is not None:
        try:
            alts = await availability_provider.find_alternatives(
                style_id, branch_id, target_date, teacher_id
            )
            if alts:
                alt = alts[0]
                schedules = await impulse_adapter.get_schedule()
                sch = next((s for s in schedules if s.id == alt.schedule_id), None)
                alt_group = (
                    (getattr(sch, "style_name", None) or
                     ((sch.group or {}).get("style") or {}).get("name"))
                    if sch else None
                ) or "другая группа"
                alt_branch = (sch.branch or {}).get("name") if sch and sch.branch else "другой филиал"
                alt_date_str = alt.date.strftime("%d.%m.%Y")
                hour, minute = (19, 30)
                if sch and sch.minutes_begin is not None:
                    hour, minute = divmod(sch.minutes_begin, 60)
                alt_time_str = f"{hour:02d}:{minute:02d}"
                tz = ZoneInfo("Asia/Vladivostok")
                new_dt = datetime.combine(alt.date, time(hour, minute), tzinfo=tz)
                await update_slots(
                    session,
                    confirmed=False,
                    schedule_id=str(alt.schedule_id),
                    datetime_resolved=new_dt,
                    group=alt_group,
                    branch=alt_branch,
                )
                return f"Все ближайшие даты в этой группе закрыты. Есть {alt_group} в {alt_branch}, {alt_date_str} в {alt_time_str}. Подойдёт?"
        except Exception as e:
            logger.warning("find_alternatives failed: %s", e)

    style_name = slots.group or "это направление"
    phone = slots.client_phone or ""
    reason = f"Лист ожидания: {phone}, {style_name}, {slots.branch or ''}"
    await update_slots(
        session,
        confirmed=False,
        escalation_pending_reason=reason,
    )
    return "Сейчас все группы по этому направлению закрыты для записи. Передать администратору, чтобы добавили тебя в лист ожидания?"
