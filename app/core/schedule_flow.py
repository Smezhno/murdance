"""Schedule fetching and formatting.

Deterministic schedule display — no LLM calls (CONTRACT §6, no hallucination).
RFC-005: availability markers when provider is set.
"""

import logging
import re
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from app.core.availability.protocol import AvailabilityStatus
from app.core.availability.schedule_expander import expand_schedule
from app.integrations.impulse.models import impulse_day_to_weekday
from app.storage.postgres import postgres_storage

logger = logging.getLogger(__name__)
_STUDIO_TZ = ZoneInfo("Asia/Vladivostok")

_MONTH_RU = ("янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")

_AVAILABILITY_MARKERS = {
    AvailabilityStatus.OPEN: "✅",
    AvailabilityStatus.CLOSED: "❌ ЗАКРЫТО",
    AvailabilityStatus.PRIORITY: "⭐ НОВАЯ ХОРЕОГРАФИЯ",
    AvailabilityStatus.HOLIDAY: "🚫 ВЫХОДНОЙ",
    AvailabilityStatus.INFO: "",
}

DAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

SCHEDULE_KEYWORDS = (
    "расписание", "какое расписание", "покажи расписание",
    "какие занятия есть", "какие направления", "когда занятия",
    "во сколько занятия", "дни занятий", "есть ли занятия",
    "а какие есть", "какие есть", "что есть", "что у вас есть",
    "что там есть", "что за занятия", "какие стили", "какие варианты",
    "что вы предлагаете", "что предлагаете",
)

_DAY_ALIASES: dict[str, int] = {
    "пн": 0, "понедельник": 0,
    "вт": 1, "вторник": 1,
    "ср": 2, "среда": 2, "среду": 2,
    "чт": 3, "четверг": 3,
    "пт": 4, "пятница": 4, "пятницу": 4,
    "сб": 5, "суббота": 5, "субботу": 5,
    "вс": 6, "воскресенье": 6,
}

_ORDINALS: dict[str, int] = {
    "перв": 1, "втор": 2, "трет": 3, "четв": 4,
    "пят": 5, "шест": 6, "седьм": 7,
}


def is_schedule_query(text: str) -> bool:
    """Return True if text matches any schedule keyword."""
    lower = text.lower().strip()
    return any(kw in lower for kw in SCHEDULE_KEYWORDS)


async def fetch_schedule(
    impulse_adapter: Any,
    slots: dict[str, Any],
    trace_id: UUID,
) -> list | dict:
    """Fetch schedule from CRM and log the tool call (CONTRACT §17).

    Returns list of Schedule objects on success, {"error": str} on failure.
    """
    start = time.monotonic()
    date_from_str: str | None = slots.get("datetime")
    date_from: date | None = None
    if date_from_str:
        try:
            date_from = date.fromisoformat(date_from_str)
        except (ValueError, AttributeError):
            pass

    try:
        schedules = await impulse_adapter.get_schedule(date_from=date_from)
        duration_ms = int((time.monotonic() - start) * 1000)
        await postgres_storage.log_tool_call(
            trace_id=trace_id,
            tool_name="get_schedule",
            parameters={"date_from": date_from_str} if date_from_str else {},
            result={"count": len(schedules)},
            duration_ms=duration_ms,
        )
        return schedules
    except Exception as e:
        duration_ms = int((time.monotonic() - start) * 1000)
        await postgres_storage.log_tool_call(
            trace_id=trace_id,
            tool_name="get_schedule",
            parameters={"date_from": date_from_str} if date_from_str else {},
            error=str(e),
            duration_ms=duration_ms,
        )
        return {"error": str(e)}


def _resolve_group_filter(
    schedules: list,
    group_filter: str | None,
    message_text: str,
) -> str | None:
    """Resolve group_filter from slots or message text against known style names."""
    if group_filter:
        return group_filter
    msg_lower = message_text.lower()
    for s in schedules:
        if hasattr(s, "style_name") and s.style_name.lower() in msg_lower:
            return s.style_name
    return None


def build_schedule_groups(
    schedules: list,
    group_filter: str | None = None,
    message_text: str = "",
) -> list[dict]:
    """Build ordered list of teacher groups for parse_schedule_choice.

    Groups CRM schedule entries by (teacher_name, minutes_begin) so the same
    teacher at the same time on multiple days collapses into one entry.

    Returns:
        [{"teacher": str, "days": list[int], "time_str": str, "minutes_begin": int | None}]
        Same order as format_schedule output (1-indexed for parse_schedule_choice).
    """
    group_filter = _resolve_group_filter(schedules, group_filter, message_text)

    filtered = [
        s for s in schedules
        if hasattr(s, "style_name") and (
            group_filter is None or s.style_name.lower() == group_filter.lower()
        )
    ]

    buckets: dict[tuple, list[int]] = defaultdict(list)
    meta: dict[tuple, Any] = {}
    for s in filtered:
        key = (s.teacher_name or "—", s.minutes_begin)
        if s.day is not None:
            buckets[key].append(s.day)
        meta[key] = s

    result = []
    for (teacher_name, minutes_begin), days in buckets.items():
        s = meta[(teacher_name, minutes_begin)]
        if minutes_begin is not None:
            h, m = divmod(minutes_begin, 60)
            time_str = f"{h:02d}:{m:02d}"
        else:
            time_str = s.time_str if hasattr(s, "time_str") else "?"
        result.append({
            "teacher": teacher_name,
            "days": sorted(days),
            "time_str": time_str,
            "minutes_begin": minutes_begin,
            "style": getattr(s, "style_name", None) or "",
        })
    return result


def _teacher_name_matches(filter_str: str | None, full_name: str | None) -> bool:
    """True if filter matches full name (exact substring or diminutive stem, e.g. Настя → Анастасия)."""
    if not filter_str or not full_name:
        return bool(not filter_str)
    fl = filter_str.strip().lower()
    nl = (full_name or "").strip().lower()
    if fl in nl:
        return True
    # Diminutive vs full name: "настя" vs "анастасия" — stem of 3–4 chars often matches
    if len(fl) >= 3 and fl[:3] in nl:
        return True
    if len(fl) >= 4 and fl[:4] in nl:
        return True
    return False


def format_schedule(
    schedules: list,
    group_filter: str | None = None,
    message_text: str = "",
    teacher_filter: str | None = None,
) -> str:
    """Format schedule as a numbered list grouped by teacher (deterministic, no LLM).

    Internally calls build_schedule_groups — output order matches parse_schedule_choice index.
    teacher_filter: lowercase substring to filter by teacher name.
    """
    group_filter = _resolve_group_filter(schedules, group_filter, message_text)
    groups = build_schedule_groups(schedules, group_filter=group_filter)

    if teacher_filter:
        groups = [g for g in groups if _teacher_name_matches(teacher_filter, g.get("teacher"))]

    if not groups:
        return "На ближайшие дни занятий не найдено. Уточните у администратора."

    if teacher_filter:
        # Show teacher name + style in header
        teacher_name = groups[0]["teacher"] if groups else teacher_filter
        header = f"Расписание {teacher_name}:"
    else:
        header = f"Расписание {group_filter}:" if group_filter else "Актуальное расписание:"
    lines = [header, ""]
    for idx, g in enumerate(groups, start=1):
        days_str = ", ".join(
            DAYS_RU[impulse_day_to_weekday(d)] for d in g["days"] if 1 <= d <= 7
        )
        style_part = f" ({g.get('style', '')})" if teacher_filter and g.get("style") else ""
        lines.append(f"{idx}. {g['teacher']}{style_part} — {days_str}, {g['time_str']}")

    return "\n".join(lines)


async def _format_schedule_with_availability(
    schedules: list,
    slots: dict[str, Any],
    availability_provider: Any,
    group_filter: str | None,
    teacher_filter: str | None,
    message_text: str,
    pre_filtered: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Format schedule with concrete dates and availability markers (RFC-005 §7.2).

    Returns:
        (formatted_text, availability_cache). Cache key: f"{schedule_id}:{date}".
        availability_cache is used by G14 guardrail to block booking on CLOSED/HOLIDAY groups.
    When pre_filtered=True, skips group_filter string matching (schedules already filtered by
    style_id/branch_id/teacher_id; KB name != CRM name e.g. "High Heels" vs "Хай хиллс").
    """
    from app.core.availability.protocol import GroupAvailability

    if not pre_filtered:
        group_filter = _resolve_group_filter(schedules, group_filter, message_text)
        filtered = [
            s for s in schedules
            if hasattr(s, "style_name") and (
                group_filter is None or s.style_name.lower() == (group_filter or "").lower()
            )
        ]
    else:
        filtered = list(schedules)
    if teacher_filter:
        filtered = [s for s in filtered if _teacher_name_matches(teacher_filter, getattr(s, "teacher_name", None))]
    if not filtered:
        return "На ближайшие дни занятий не найдено. Уточните у администратора.", {}, None

    from_date = datetime.now(_STUDIO_TZ).date()
    to_date = from_date + timedelta(days=14)
    expanded = expand_schedule(filtered, from_date, to_date)

    # Debug: verify expanded schedule matches CRM templates (Impulse day 0=Mon?)
    for tmpl in filtered[:5]:
        logger.info(
            "CRM_TEMPLATE: schedule_id=%s day=%s (0=Mon) minutes_begin=%s style=%s teacher=%s",
            getattr(tmpl, "id", None),
            getattr(tmpl, "day", None),
            getattr(tmpl, "minutes_begin", None),
            getattr(tmpl, "style_name", None),
            getattr(tmpl, "teacher_name", None),
        )
    for slot in expanded[:5]:
        logger.info(
            "EXPANDED: schedule_id=%s date=%s weekday=%s time=%s group=%s teacher=%s",
            slot.schedule_id,
            slot.date,
            slot.date.weekday(),
            slot.time_begin,
            slot.group_name,
            slot.teacher_name,
        )

    lines: list[str] = []
    cache: dict[str, GroupAvailability] = {}
    header = f"Расписание {group_filter}:" if group_filter else "Актуальное расписание:"
    if teacher_filter:
        header = f"Расписание {teacher_filter}:"
    lines.append(header)
    lines.append("")

    crm_available = True
    for slot in expanded:
        if not crm_available:
            marker = "✅"
        else:
            try:
                avail = await availability_provider.get_availability(slot.schedule_id, slot.date)
                cache[f"{slot.schedule_id}:{slot.date.isoformat()}"] = avail
                marker = _AVAILABILITY_MARKERS.get(avail.status, "✅")
            except Exception as e:
                logger.warning("availability_fetch_failed slot=%s: %s", slot.schedule_id, e)
                crm_available = False
                marker = "✅"
        day_short = DAYS_RU[slot.date.weekday()] if 0 <= slot.date.weekday() <= 6 else "?"
        date_str = f"{slot.date.day} {_MONTH_RU[slot.date.month - 1]}"
        time_str = slot.time_begin.strftime("%H:%M") if hasattr(slot.time_begin, "strftime") else "?"
        group_name = slot.group_name or "?"
        teacher_name = slot.teacher_name or "—"
        branch_name = slot.branch_name or "—"
        marker_part = f" | {marker}" if marker else ""
        lines.append(f"{day_short} {date_str} {time_str} | {group_name} | {teacher_name} | {branch_name}{marker_part}")

    first_slot: dict[str, Any] | None = None
    if expanded:
        first = expanded[0]
        first_slot = {
            "schedule_id": first.schedule_id,
            "date": first.date,
            "time": first.time_begin,
        }
    return "\n".join(lines), cache, first_slot


def _entry_style_id(entry: Any) -> Any:
    """Extract style ID from schedule entry. Schedule has group.style.id, not top-level style_id."""
    if hasattr(entry, "group") and isinstance(entry.group, dict):
        style = entry.group.get("style")
        if isinstance(style, dict):
            return style.get("id")
        if style is not None and hasattr(style, "id"):
            return getattr(style, "id", None)
    return None


def _entry_branch_id(entry: Any) -> Any:
    """Extract branch ID from schedule entry."""
    if hasattr(entry, "branch") and isinstance(entry.branch, dict):
        return entry.branch.get("id")
    return getattr(entry, "branch_id", None)


def _entry_teacher_id(entry: Any) -> Any:
    """Extract teacher ID from schedule entry (group.teacher1.id)."""
    if hasattr(entry, "group") and isinstance(entry.group, dict):
        t1 = entry.group.get("teacher1")
        if isinstance(t1, dict):
            return t1.get("id")
        if t1 is not None and hasattr(t1, "id"):
            return getattr(t1, "id", None)
    return getattr(entry, "teacher_id", None)


def _matches_filter(
    entry: Any,
    style_id: Any,
    branch_id: Any,
    teacher_id: Any,
    group_name: str | None,
    branch_name: str | None,
    teacher_name: str | None,
) -> bool:
    """Filter schedule entry. IDs take priority over string names (RFC-006 Phase 2).
    When a filter ID is set but entry has no such ID, entry is excluded (no silent pass).
    """
    # Style filter (Schedule has group.style.id, not entry.style_id)
    if style_id is not None:
        entry_style_id = _entry_style_id(entry)
        if entry_style_id is None:
            return False
        if str(entry_style_id) != str(style_id):
            return False
    elif group_name:
        entry_name = getattr(entry, "style_name", "") or ""
        if group_name.lower() not in entry_name.lower():
            return False

    # Branch filter
    if branch_id is not None:
        entry_branch_id = _entry_branch_id(entry)
        if entry_branch_id is None:
            return False
        if str(entry_branch_id) != str(branch_id):
            return False
    elif branch_name:
        entry_branch = getattr(entry, "branch_name", "") or ""
        if branch_name.lower() not in entry_branch.lower():
            return False

    # Teacher filter (Schedule has group.teacher1.id)
    if teacher_id is not None:
        entry_teacher_id = _entry_teacher_id(entry)
        if entry_teacher_id is None:
            return False
        if str(entry_teacher_id) != str(teacher_id):
            return False
    elif teacher_name:
        entry_teacher = getattr(entry, "teacher_name", "") or ""
        if teacher_name.lower() not in entry_teacher.lower():
            return False

    return True


async def generate_schedule_response(
    impulse_adapter: Any,
    slots: dict[str, Any],
    trace_id: UUID,
    message_text: str = "",
    availability_provider: Any = None,
) -> tuple[str, dict[str, Any]]:
    """Fetch from CRM and return formatted schedule string. No LLM.

    Args:
        impulse_adapter: ImpulseAdapter instance.
        slots: Current booking slots dict (may contain "group", "datetime").
        trace_id: Trace ID for logging.
        message_text: Raw user message text (str) for style detection fallback.
        availability_provider: Optional. When set, shows concrete dates with ✅/❌/⭐ markers.

    Returns:
        (formatted_text, availability_cache). Cache key: f"{schedule_id}:{date}".
        Empty dict when no availability_provider.
    """
    schedules = await fetch_schedule(impulse_adapter, slots, trace_id)
    if isinstance(schedules, dict) and "error" in schedules:
        return "Не удалось получить расписание. Попробуйте позже или свяжитесь с администратором.", {}, None
    if not schedules:
        return "На ближайшие дни занятий не найдено. Уточните у администратора.", {}, None

    if schedules:
        s = schedules[0]
        print(f"SCHEDULE_TYPE: {type(s)}")
        print(f"SCHEDULE_DICT: {s.model_dump() if hasattr(s, 'model_dump') else vars(s)}")
        print(f"HAS_GROUP_DICT: {hasattr(s, 'group') and isinstance(getattr(s, 'group', None), dict)}")
        print(f"HAS_STYLE_ID: {hasattr(s, 'style_id')}")
        print(f"HAS_TEACHER_ID: {hasattr(s, 'teacher_id')}")
        print(f"GROUP_VALUE: {getattr(s, 'group', 'NO_ATTR')}")
        print(f"BRANCH_VALUE: {getattr(s, 'branch', 'NO_ATTR')}")

    style_id = slots.get("style_id")
    branch_id = slots.get("branch_id")
    teacher_id = slots.get("teacher_id")
    group_name = (slots.get("group") or "").strip() or None
    branch_name = (slots.get("branch") or "").strip() or None
    teacher_name = (slots.get("teacher") or "").strip() or None

    if schedules:
        print(
            f"ENTRY_ATTRS: style_id={hasattr(schedules[0], 'style_id')} "
            f"teacher_id={hasattr(schedules[0], 'teacher_id')}"
        )
    for i, entry in enumerate(schedules[:3]):
        print(
            f"MATCH_DEBUG: entry[{i}] has style_id={hasattr(entry, 'style_id')} "
            f"group={type(getattr(entry, 'group', None)).__name__} "
            f"extracted_style_id={_entry_style_id(entry)} "
            f"branch_id={_entry_branch_id(entry)} "
            f"teacher_id={_entry_teacher_id(entry)}"
        )

    filtered = [
        s
        for s in schedules
        if _matches_filter(
            s, style_id, branch_id, teacher_id,
            group_name, branch_name, teacher_name,
        )
    ]
    print(
        f"SCHEDULE_FILTER: total={len(schedules)} style_id={style_id} branch_id={branch_id} "
        f"teacher_id={teacher_id} → {len(filtered)} matches"
    )
    schedules = filtered
    if not schedules:
        return "На ближайшие дни занятий не найдено. Уточните у администратора.", {}, None

    was_filtered = (
        style_id is not None or branch_id is not None or teacher_id is not None
    )

    if availability_provider is not None:
        try:
            text, cache, first_slot = await _format_schedule_with_availability(
                schedules,
                slots,
                availability_provider,
                group_filter=slots.get("group"),
                teacher_filter=slots.get("teacher"),
                message_text=message_text,
                pre_filtered=was_filtered,
            )
            return text, cache, first_slot
        except Exception as e:
            logger.warning("schedule_with_availability failed, falling back: %s", e)

    text = format_schedule(
        schedules,
        group_filter=slots.get("group") if not was_filtered else None,
        message_text=message_text,
        teacher_filter=slots.get("teacher"),
    )
    return text, {}, None


def parse_schedule_choice(text: str, groups: list[dict]) -> dict | None:
    """Parse user's response after a schedule display.

    Args:
        text: Raw user message.
        groups: Ordered list of dicts {teacher, days: list[int], time_str, minutes_begin}.
                Order must match the numbered lines produced by format_schedule.

    Returns:
        {"teacher": str, "day": int | None, "time_str": str} or None if unclear.
        day is None when teacher is identified but multiple days are possible.
    """
    lower = text.strip().lower()

    def _pick(g: dict, day: int | None = None) -> dict:
        return {"teacher": g["teacher"], "day": day, "time_str": g["time_str"]}

    # 1. Digit: "1", "2" …
    m = re.search(r"\b(\d+)\b", lower)
    if m:
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(groups):
            g = groups[idx]
            return _pick(g, g["days"][0] if len(g["days"]) == 1 else None)

    # 2. Day-of-week mention (checked BEFORE ordinals to avoid "вторник" → "второй" collision)
    for word, day_num in _DAY_ALIASES.items():
        if re.search(rf"\b{re.escape(word)}\b", lower):
            candidates = [g for g in groups if day_num in g["days"]]
            if len(candidates) == 1:
                return _pick(candidates[0], day_num)
            # Multiple teachers on that day — ambiguous, return None (caller must ask)
            break

    # 3. Ordinal words: "первый", "второй" … (after day check)
    # Guard: skip if the matched word IS a day-of-week alias (e.g. "вторник" contains "втор")
    _day_alias_set = set(_DAY_ALIASES.keys())
    for prefix, num in _ORDINALS.items():
        for match in re.finditer(prefix, lower):
            full_word = re.search(r"\w+", lower[match.start()])
            # Extract the full token at match position
            token_match = re.search(r"\w+", lower[match.start():])
            if token_match and token_match.group(0) in _day_alias_set:
                continue  # this token is a day name, not an ordinal
            idx = num - 1
            if 0 <= idx < len(groups):
                g = groups[idx]
                return _pick(g, g["days"][0] if len(g["days"]) == 1 else None)
            break

    # 4. Teacher name partial match — handles Russian inflection by comparing
    #    first 3 chars (enough to distinguish names; Russian names share root across cases).
    #    "Даше" → "даш", "Даша" → "даш" ✓  |  "Катю" → "кат", "Катя" → "кат" ✓
    user_tokens = [t for t in re.split(r"\W+", lower) if len(t) >= 3]
    for g in groups:
        name_tokens = [t for t in g["teacher"].lower().split() if len(t) >= 3]
        for ut in user_tokens:
            for nt in name_tokens:
                stem_len = min(3, len(ut), len(nt))
                if ut[:stem_len] == nt[:stem_len]:
                    return _pick(g, g["days"][0] if len(g["days"]) == 1 else None)

    return None
