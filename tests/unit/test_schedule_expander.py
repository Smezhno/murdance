"""Unit tests for ScheduleExpander (RFC-005 §6)."""

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from app.core.availability.schedule_expander import ExpandedSlot, expand_schedule
from app.integrations.impulse.models import Schedule, impulse_day_to_weekday


def test_impulse_day_to_weekday() -> None:
    """Impulse 1=Mon..7=Sun maps to Python 0=Mon..6=Sun. Invalid day raises ValueError."""
    assert impulse_day_to_weekday(1) == 0  # Mon
    assert impulse_day_to_weekday(5) == 4  # Fri
    assert impulse_day_to_weekday(7) == 6  # Sun
    with pytest.raises(ValueError, match="Invalid Impulse day: 0"):
        impulse_day_to_weekday(0)
    with pytest.raises(ValueError, match="Invalid Impulse day: 8"):
        impulse_day_to_weekday(8)


def _schedule_regular_friday() -> Schedule:
    """Schedule: every Friday 19:30 (day=5=Fri in Impulse 1=Mon, minutesBegin=1170)."""
    return Schedule(
        id=117,
        regular=True,
        day=5,
        minutesBegin=1170,
        group={"name": "Frame Up Strip", "style": {"id": 5, "name": "Strip"}},
        branch={"id": 1, "name": "Гоголя"},
    )


def _schedule_one_time(march_5_ts: int) -> Schedule:
    """One-time schedule on March 5."""
    return Schedule(
        id=118,
        regular=False,
        dateBegin=march_5_ts,
        minutesBegin=1200,
        group={"name": "Hills", "style": {"id": 1, "name": "High Heels"}},
        branch={"id": 2, "name": "Семёновская"},
    )


def test_regular_weekly() -> None:
    """Mon-Sun range: expect 1 slot on Friday, time_begin=19:30."""
    tmpl = _schedule_regular_friday()
    from_d = date(2026, 3, 2)  # Monday
    to_d = date(2026, 3, 8)  # Sunday
    slots = expand_schedule([tmpl], from_d, to_d)
    assert len(slots) == 1
    assert slots[0].schedule_id == 117
    assert slots[0].date == date(2026, 3, 6)  # Friday
    assert slots[0].time_begin == time(19, 30)
    assert slots[0].group_name == "Frame Up Strip"


def test_regular_two_weeks() -> None:
    """14-day range: expect 2 slots (two Fridays)."""
    tmpl = _schedule_regular_friday()
    from_d = date(2026, 3, 2)
    to_d = date(2026, 3, 15)
    slots = expand_schedule([tmpl], from_d, to_d)
    assert len(slots) == 2
    assert slots[0].date == date(2026, 3, 6)
    assert slots[1].date == date(2026, 3, 13)


def test_one_time_in_range() -> None:
    """One-time Schedule with dateBegin in range → 1 slot (ts in Asia/Vladivostok)."""
    ts = int(datetime(2026, 3, 5, 12, 0, tzinfo=ZoneInfo("Asia/Vladivostok")).timestamp())
    tmpl = _schedule_one_time(ts)
    slots = expand_schedule([tmpl], date(2026, 3, 1), date(2026, 3, 10))
    assert len(slots) == 1
    assert slots[0].date == date(2026, 3, 5)
    assert slots[0].time_begin == time(20, 0)


def test_one_time_out_of_range() -> None:
    """dateBegin outside range → 0 slots."""
    ts = int(datetime(2026, 3, 5, 12, 0, tzinfo=ZoneInfo("Asia/Vladivostok")).timestamp())
    tmpl = _schedule_one_time(ts)
    slots = expand_schedule([tmpl], date(2026, 3, 10), date(2026, 3, 20))
    assert len(slots) == 0


def test_mixed() -> None:
    """1 regular + 1 one-time → correct count, sorted by date."""
    ts = int(datetime(2026, 3, 5, 12, 0, tzinfo=ZoneInfo("Asia/Vladivostok")).timestamp())
    regular = _schedule_regular_friday()
    one_time = _schedule_one_time(ts)
    slots = expand_schedule([regular, one_time], date(2026, 3, 1), date(2026, 3, 15))
    assert len(slots) == 3  # March 5 (one-time), March 6 (Fri), March 13 (Fri)
    assert slots[0].date == date(2026, 3, 5)
    assert slots[1].date == date(2026, 3, 6)
    assert slots[2].date == date(2026, 3, 13)


def test_sort_order() -> None:
    """Multiple templates on same date → sorted by time_begin."""
    # Friday 19:30 and Friday 18:00 (day=5=Fri in Impulse)
    early = Schedule(id=101, regular=True, day=5, minutesBegin=1080, group={}, branch=None)
    late = Schedule(id=102, regular=True, day=5, minutesBegin=1170, group={}, branch=None)
    slots = expand_schedule([late, early], date(2026, 3, 2), date(2026, 3, 8))
    assert len(slots) == 2
    assert slots[0].time_begin == time(18, 0)
    assert slots[1].time_begin == time(19, 30)


def test_no_templates() -> None:
    """Empty list → empty result."""
    slots = expand_schedule([], date(2026, 3, 1), date(2026, 3, 31))
    assert slots == []


def test_null_fields() -> None:
    """Schedule with regular=None, date_begin=None → skip gracefully."""
    tmpl = Schedule(id=99, regular=None, date_begin=None, group=None, branch=None)
    slots = expand_schedule([tmpl], date(2026, 3, 1), date(2026, 3, 31))
    assert len(slots) == 0
