"""Schedule expander — unfolds templates into concrete dates (RFC-005 §6)."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, time
from zoneinfo import ZoneInfo

from app.core.availability.protocol import AvailabilityStatus
from app.integrations.impulse.models import Schedule, impulse_day_to_weekday

_STUDIO_TZ = ZoneInfo("Asia/Vladivostok")


@dataclass
class ExpandedSlot:
    schedule_id: int
    date: date
    time_begin: time
    time_end: time
    group_name: str
    teacher_name: str | None
    branch_name: str | None
    style_id: int | None
    branch_id: int | None
    teacher_id: int | None
    availability: AvailabilityStatus = AvailabilityStatus.OPEN


def _minutes_to_time(minutes: int | None) -> time:
    """Convert minutes from midnight to time. Returns 00:00 if None."""
    if minutes is None:
        return time(0, 0)
    h, m = divmod(minutes, 60)
    return time(h, m)


def expand_schedule(
    templates: list[Schedule],
    from_date: date,
    to_date: date,
) -> list[ExpandedSlot]:
    """Expand schedule templates to concrete dates. Impulse day 1=Mon maps via impulse_day_to_weekday()."""
    slots: list[ExpandedSlot] = []
    for tmpl in templates:
        if tmpl.regular and tmpl.day is not None:
            python_weekday = impulse_day_to_weekday(tmpl.day)
            current = from_date
            while current <= to_date:
                if current.weekday() == python_weekday:
                    slots.append(_make_slot(tmpl, current))
                current += timedelta(days=1)
        elif not tmpl.regular and tmpl.date_begin is not None:
            slot_date = datetime.fromtimestamp(tmpl.date_begin, tz=_STUDIO_TZ).date()
            if from_date <= slot_date <= to_date:
                slots.append(_make_slot(tmpl, slot_date))
    slots.sort(key=lambda s: (s.date, s.time_begin))
    return slots


def _make_slot(tmpl: Schedule, slot_date: date) -> ExpandedSlot:
    t_begin = _minutes_to_time(tmpl.minutes_begin)
    t_end = _minutes_to_time(tmpl.minutes_end) if tmpl.minutes_end is not None else _minutes_to_time(
        (tmpl.minutes_begin or 0) + 60
    )
    g = tmpl.group or {}
    b = tmpl.branch or {}
    group_name = (g.get("name") or tmpl.style_name) or "?"
    t1 = g.get("teacher1") if isinstance(g.get("teacher1"), dict) else None
    if t1:
        first = (t1.get("name") or "").strip()
        last = (t1.get("lastName") or "").strip()
        teacher_name = f"{first} {last}".strip() if last else (first or None)
    else:
        teacher_name = None
    branch_name = b.get("name") if isinstance(b, dict) else None
    style_id = g.get("style", {}).get("id") if isinstance(g.get("style"), dict) else None
    branch_id = b.get("id") if isinstance(b, dict) else None
    teacher_id = g.get("teacher1", {}).get("id") if isinstance(g.get("teacher1"), dict) else None
    return ExpandedSlot(
        schedule_id=tmpl.id,
        date=slot_date,
        time_begin=t_begin,
        time_end=t_end,
        group_name=group_name,
        teacher_name=teacher_name,
        branch_name=branch_name,
        style_id=style_id,
        branch_id=branch_id,
        teacher_id=teacher_id,
    )
