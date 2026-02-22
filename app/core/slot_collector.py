"""Slot collection logic for booking flow.

Handles: slot state checks, datetime parsing, teacher×day validation,
schedule-mid-booking queries, and asking for missing slots via LLM.
No hardcoded response strings — all user-facing text goes through response_generator.
"""

import logging
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from app.core.conversation import update_slots
from app.core.response_generator import generate_response
from app.core.schedule_flow import (
    build_schedule_groups,
    format_schedule,
    is_schedule_query,
    parse_schedule_choice,
)
from app.models import ConversationState

logger = logging.getLogger(__name__)

DAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def all_slots_filled(session: Any) -> bool:
    s = session.slots
    return bool(s.group and s.datetime_resolved and s.client_name and s.client_phone)


def get_missing_slots(session: Any) -> list[str]:
    s = session.slots
    missing = []
    if not s.group: missing.append("направление")
    if not s.datetime_resolved: missing.append("дата и время")
    if not s.client_name: missing.append("имя")
    if not s.client_phone: missing.append("телефон")
    return missing


async def _ask_next_slot(missing: list[str], session: Any, trace_id: UUID, history: list | None) -> str:
    if "направление" in missing:
        return await generate_response("ask_direction", {}, trace_id, history)
    if "дата и время" in missing:
        return await generate_response("ask_datetime", {"group": session.slots.group or ""}, trace_id, history)
    return await generate_response("ask_contact", {}, trace_id, history)


async def validate_teacher_day(
    session: Any,
    impulse_adapter: Any,
    kb: Any,
    cached_schedules: list | None = None,
) -> str | None:
    """Verify teacher teaches on chosen day; fix time or clear datetime + return error.

    Pass cached_schedules to skip a second CRM call in the same turn.
    Returns None if valid or not enough info to check.
    """
    teacher = session.slots.teacher
    dt = session.slots.datetime_resolved
    if not teacher or not dt:
        return None

    target_weekday = dt.weekday()
    teacher_lower = teacher.lower()
    try:
        schedules = cached_schedules if cached_schedules is not None else await impulse_adapter.get_schedule()
    except Exception:
        return None

    teacher_schedules = [s for s in schedules if s.teacher_name and teacher_lower in s.teacher_name.lower()]
    if not teacher_schedules:
        return None

    teacher_days = {s.day for s in teacher_schedules}
    if target_weekday not in teacher_days:
        available_str = "-".join(DAYS_RU[d] for d in sorted(teacher_days) if d is not None)
        await update_slots(session, datetime_resolved=None, datetime_raw=None)
        return await generate_response(
            "ask_datetime",
            {"group": session.slots.group or teacher,
             "note": f"У {teacher} занятия: {available_str}"},
            session.trace_id,
        )

    # Snap time to match actual schedule entry
    matching = next((s for s in teacher_schedules if s.day == target_weekday), None)
    if matching and matching.minutes_begin is not None:
        h, m = divmod(matching.minutes_begin, 60)
        corrected = dt.replace(hour=h, minute=m, second=0, microsecond=0)
        if corrected != dt:
            await update_slots(session, datetime_resolved=corrected)
    return None


def _resolve_days_ahead(day: int, today: datetime, time_str: str) -> int:
    """Return days until next occurrence of `day` (0=Mon), respecting same-day classes.

    If today is the target weekday, checks whether the class time has already passed.
    If not passed yet → 0 (today). If passed → 7 (next week).
    """
    days_ahead = (day - today.weekday()) % 7
    if days_ahead == 0:
        try:
            h, m = map(int, time_str.split(":"))
            class_time = today.replace(hour=h, minute=m, second=0, microsecond=0)
            if today >= class_time:
                days_ahead = 7
        except ValueError:
            days_ahead = 7
    return days_ahead


async def _apply_schedule_choice(
    message_text: str, session: Any, kb: Any, schedules: list
) -> bool:
    """Parse a numbered/named schedule choice from user message and apply to session.

    Accepts pre-fetched schedules — no CRM call inside.
    Returns True if a choice was parsed and slots were updated.
    Only runs when group is known but datetime is still missing.
    """
    if not session.slots.group or session.slots.datetime_resolved:
        return False
    try:
        groups = build_schedule_groups(schedules, group_filter=session.slots.group)
        choice = parse_schedule_choice(message_text, groups)
        if not choice:
            return False
        await update_slots(session, teacher=choice["teacher"])
        day = choice.get("day")
        time_str = choice.get("time_str", "")
        if day is not None and time_str:
            tz = ZoneInfo(kb.studio.timezone)
            today = datetime.now(tz)
            days_ahead = _resolve_days_ahead(day, today, time_str)
            target = today.date() + timedelta(days=days_ahead)
            h, m = map(int, time_str.split(":"))
            resolved = datetime(target.year, target.month, target.day, h, m, tzinfo=tz)
            await update_slots(session, datetime_resolved=resolved, datetime_raw=f"{DAYS_RU[day]}, {time_str}")
        return True
    except Exception:
        logger.debug("_apply_schedule_choice failed", exc_info=True)
        return False


async def collect_slots(
    message: Any,
    session: Any,
    trace_id: UUID,
    conversation_history: list[dict] | None,
    impulse_adapter: Any,
    temporal_parser: Any,
    kb: Any,
) -> str:
    """Process one user message during slot collection.

    Fetches CRM schedule AT MOST ONCE per call, reuses across all steps.

    Order of operations:
    1. Keyword schedule query → fetch once, show schedule + ask next slot
    2. LLM intent resolution
    3. LLM returned schedule_query → reuse cached schedules
    4. Try parse numbered/named schedule choice (uses cached schedules)
    5. Parse datetime from LLM-extracted text
    6. Apply non-null slots
    7. Validate teacher × day (uses cached schedules)
    8. Ask for next missing slot or return LLM response

    NOTE: Transition to CONFIRM_BOOKING is the caller's responsibility.
    After collect_slots returns, booking_flow must check all_slots_filled(session)
    and call transition_state(session, CONFIRM_BOOKING) if True.
    """
    from app.core.intent import resolve_intent

    slots_dict = session.slots.model_dump()
    _schedules: list | None = None  # lazy-loaded, shared across all steps

    async def _get_schedules() -> list:
        nonlocal _schedules
        if _schedules is None:
            _schedules = await impulse_adapter.get_schedule()
        return _schedules

    # 1. Keyword schedule query
    if is_schedule_query(message.text):
        scheds = await _get_schedules()
        sched_text = format_schedule(scheds, group_filter=slots_dict.get("group"), message_text=message.text)
        missing = get_missing_slots(session)
        if not missing:
            return sched_text
        return f"{sched_text}\n\n{await _ask_next_slot(missing, session, trace_id, conversation_history)}"

    # 2. LLM intent resolution
    intent_result = await resolve_intent(
        message, session.state.value, slots_dict, trace_id,
        conversation_history=conversation_history,
    )
    intent = intent_result.get("intent", "")
    slots = intent_result.get("slots", {})

    # 3. LLM returned schedule_query
    if intent == "schedule_query":
        scheds = await _get_schedules()
        sched_text = format_schedule(
            scheds,
            group_filter=slots.get("group") or slots_dict.get("group"),
            message_text=message.text,
        )
        missing = get_missing_slots(session)
        if not missing:
            return sched_text
        return f"{sched_text}\n\n{await _ask_next_slot(missing, session, trace_id, conversation_history)}"

    # 4. Try numbered/named schedule choice (reuses _schedules if already fetched)
    if session.slots.group and not session.slots.datetime_resolved and not slots.get("datetime"):
        await _apply_schedule_choice(message.text, session, kb, await _get_schedules())

    # 5. Parse datetime from LLM-extracted text
    if slots.get("datetime"):
        temporal_result = temporal_parser.parse(slots["datetime"])
        if temporal_result.resolved_date:
            time_str = temporal_result.time or "19:00"
            tz = ZoneInfo(kb.studio.timezone)
            slots["datetime_resolved"] = datetime.combine(
                temporal_result.resolved_date,
                datetime.strptime(time_str, "%H:%M").time(),
            ).replace(tzinfo=tz)
        slots["datetime_raw"] = slots["datetime"]

    # 6. Apply non-null slots
    non_null = {k: v for k, v in slots.items() if v is not None}
    if non_null:
        await update_slots(session, **non_null)

    # 7. Validate teacher × day (cached schedules passed — no extra CRM call)
    if err := await validate_teacher_day(session, impulse_adapter, kb, cached_schedules=_schedules):
        return err

    # 8. Ask for next missing slot or return LLM response
    missing = get_missing_slots(session)
    if missing:
        return await _ask_next_slot(missing, session, trace_id, conversation_history)
    return intent_result.get("response_text", "") or await generate_response("unclear", {}, trace_id, conversation_history)
