"""Schedule fetching and formatting.

Deterministic schedule display — no LLM calls (CONTRACT §6, no hallucination).
"""

import re
import time
from collections import defaultdict
from datetime import date
from typing import Any
from uuid import UUID

from app.storage.postgres import postgres_storage

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
        groups = [g for g in groups if teacher_filter in (g["teacher"] or "").lower()]

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
        days_str = ", ".join(DAYS_RU[d] for d in g["days"] if 0 <= d <= 6)
        style_part = f" ({g.get('style', '')})" if teacher_filter and g.get("style") else ""
        lines.append(f"{idx}. {g['teacher']}{style_part} — {days_str}, {g['time_str']}")

    return "\n".join(lines)


async def generate_schedule_response(
    impulse_adapter: Any,
    slots: dict[str, Any],
    trace_id: UUID,
    message_text: str = "",
) -> str:
    """Fetch from CRM and return formatted schedule string. No LLM.

    Args:
        impulse_adapter: ImpulseAdapter instance.
        slots: Current booking slots dict (may contain "group", "datetime").
        trace_id: Trace ID for logging.
        message_text: Raw user message text (str) for style detection fallback.
                      Pass message.text at call sites — this module never imports UnifiedMessage.
    """
    schedules = await fetch_schedule(impulse_adapter, slots, trace_id)
    if isinstance(schedules, dict) and "error" in schedules:
        return "Не удалось получить расписание. Попробуйте позже или свяжитесь с администратором."
    if not schedules:
        return "На ближайшие дни занятий не найдено. Уточните у администратора."

    # Filter by branch_id when present (RFC-004), else by branch name
    branch_id = slots.get("branch_id")
    if branch_id is not None and schedules and hasattr(schedules[0], "branch"):
        sid = str(branch_id)
        schedules = [
            s for s in schedules
            if getattr(s, "branch", None) and str((s.branch or {}).get("id")) == sid
        ]
    else:
        slot_branch = (slots.get("branch") or "").strip().lower()
        if slot_branch and schedules and hasattr(schedules[0], "branch_name"):
            s_branch_norm = slot_branch
            schedules = [
                s for s in schedules
                if getattr(s, "branch_name", None)
                and (s.branch_name.strip().lower() == s_branch_norm
                     or s_branch_norm in s.branch_name.strip().lower()
                     or s.branch_name.strip().lower() in s_branch_norm)
            ]
    # Filter by style_id when present (RFC-004)
    style_id = slots.get("style_id")
    if style_id is not None and schedules and hasattr(schedules[0], "group"):
        sid = str(style_id)
        schedules = [
            s for s in schedules
            if getattr(s, "group", None)
            and str((s.group or {}).get("style", {}).get("id")) == sid
        ]
    # Filter by teacher_id when present (RFC-004)
    teacher_id = slots.get("teacher_id")
    if teacher_id is not None and schedules and hasattr(schedules[0], "group"):
        tid = str(teacher_id)
        schedules = [
            s for s in schedules
            if getattr(s, "group", None)
            and str((s.group or {}).get("teacher1", {}).get("id")) == tid
        ]
    if not schedules:
        return "На ближайшие дни занятий не найдено. Уточните у администратора."

    return format_schedule(
        schedules,
        group_filter=slots.get("group"),
        message_text=message_text,
        teacher_filter=slots.get("teacher"),
    )


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
