"""Unit tests for ImpulseStickerProvider (RFC-005 §5.3.3)."""

from datetime import date
from unittest.mock import AsyncMock

import pytest

from app.core.availability.impulse_provider import ImpulseStickerProvider
from app.core.availability.protocol import AvailabilityStatus
from app.integrations.impulse.adapter import ImpulseAdapter
from app.integrations.impulse.models import Schedule
from app.knowledge.base import StickerMapping


@pytest.fixture
def sticker_config() -> StickerMapping:
    """Default StickerMapping matching studio.yaml."""
    return StickerMapping(
        open_keywords=["МОЖНО ПРИСОЕДИНИТЬСЯ"],
        closed_keywords=["НЕЛЬЗЯ ПРИСОЕДИНИТЬСЯ", "НЕЛЬЗЬЯ ПРИСОЕДИНИТЬСЯ", "ЗАКРЫТО", "ЗАКРЫТ"],
        priority_keywords=["НОВАЯ ХОРЕОГРАФИЯ"],
        holiday_keywords=["ВЫХОДНОЙ"],
        info_keywords=["СТАРТ", "ОТКРЫТЫЙ УРОК"],
        unknown_action="open",
    )


@pytest.fixture
def mock_adapter() -> ImpulseAdapter:
    """Adapter with mocked get_schedule (stickers embedded in schedule)."""
    adapter = AsyncMock(spec=ImpulseAdapter)
    adapter.get_schedule = AsyncMock(return_value=[])
    return adapter


@pytest.fixture
def provider(mock_adapter: ImpulseAdapter, sticker_config: StickerMapping) -> ImpulseStickerProvider:
    return ImpulseStickerProvider(adapter=mock_adapter, config=sticker_config)


# --- get_availability ---


@pytest.mark.asyncio
async def test_get_availability_no_stickers(provider: ImpulseStickerProvider, mock_adapter: ImpulseAdapter) -> None:
    mock_adapter.get_schedule.return_value = []
    result = await provider.get_availability(117, date(2026, 3, 7))
    assert result.status == AvailabilityStatus.OPEN
    assert result.schedule_id == 117
    assert result.date == date(2026, 3, 7)


@pytest.mark.asyncio
async def test_get_availability_closed(provider: ImpulseStickerProvider, mock_adapter: ImpulseAdapter) -> None:
    sch = Schedule(
        id=117,
        regular=True,
        day=5,
        minutesBegin=1170,
        group={},
        branch=None,
        sticker={"name": "НЕЛЬЗЯ ПРИСОЕДИНИТЬСЯ"},
    )
    mock_adapter.get_schedule.return_value = [sch]
    result = await provider.get_availability(117, date(2026, 3, 7))
    assert result.status == AvailabilityStatus.CLOSED
    assert result.sticker_text == "НЕЛЬЗЯ ПРИСОЕДИНИТЬСЯ"


@pytest.mark.asyncio
async def test_get_availability_open(provider: ImpulseStickerProvider, mock_adapter: ImpulseAdapter) -> None:
    sch = Schedule(
        id=117,
        regular=True,
        day=5,
        minutesBegin=1170,
        group={},
        branch=None,
        sticker={"name": "МОЖНО ПРИСОЕДИНИТЬСЯ"},
    )
    mock_adapter.get_schedule.return_value = [sch]
    result = await provider.get_availability(117, date(2026, 3, 7))
    assert result.status == AvailabilityStatus.OPEN


@pytest.mark.asyncio
async def test_get_availability_filters_by_schedule_id(
    provider: ImpulseStickerProvider, mock_adapter: ImpulseAdapter
) -> None:
    sch_other = Schedule(
        id=99,
        regular=True,
        day=5,
        minutesBegin=1170,
        group={},
        branch=None,
        sticker={"name": "НЕЛЬЗЯ ПРИСОЕДИНИТЬСЯ"},
    )
    sch_target = Schedule(
        id=117,
        regular=True,
        day=5,
        minutesBegin=1170,
        group={},
        branch=None,
        sticker={"name": "МОЖНО ПРИСОЕДИНИТЬСЯ"},
    )
    mock_adapter.get_schedule.return_value = [sch_other, sch_target]
    result = await provider.get_availability(117, date(2026, 3, 7))
    assert result.status == AvailabilityStatus.OPEN
    assert result.schedule_id == 117


@pytest.mark.asyncio
async def test_get_availability_propagates_crm_error(
    provider: ImpulseStickerProvider, mock_adapter: ImpulseAdapter
) -> None:
    """CRM/network errors propagate so schedule_flow can set crm_available=False."""
    mock_adapter.get_schedule.side_effect = RuntimeError("CRM down")
    with pytest.raises(RuntimeError, match="CRM down"):
        await provider.get_availability(117, date(2026, 3, 7))


# --- find_next_open ---


def _schedule_friday(schedule_id: int = 117) -> Schedule:
    return Schedule(id=schedule_id, regular=True, day=5, minutesBegin=1170, group={}, branch=None)


@pytest.mark.asyncio
async def test_find_next_open_immediate(
    provider: ImpulseStickerProvider, mock_adapter: ImpulseAdapter
) -> None:
    mock_adapter.get_schedule.return_value = [_schedule_friday()]
    from_d = date(2026, 3, 2)
    result = await provider.find_next_open(117, from_d, max_weeks=4)
    assert result is not None
    assert result.status == AvailabilityStatus.OPEN
    assert result.date == date(2026, 3, 6)


@pytest.mark.asyncio
async def test_find_next_open_skip_closed(
    provider: ImpulseStickerProvider, mock_adapter: ImpulseAdapter
) -> None:
    # With a CLOSED sticker on the schedule template, no open dates are found.
    closed_schedule = Schedule(
        id=117,
        regular=True,
        day=5,
        minutesBegin=1170,
        group={},
        branch=None,
        sticker={"name": "НЕЛЬЗЯ ПРИСОЕДИНИТЬСЯ"},
    )
    mock_adapter.get_schedule.return_value = [closed_schedule]
    from_d = date(2026, 3, 2)
    result = await provider.find_next_open(117, from_d, max_weeks=4)
    assert result is None


@pytest.mark.asyncio
async def test_find_next_open_skips_from_date_itself(
    provider: ImpulseStickerProvider, mock_adapter: ImpulseAdapter
) -> None:
    """When from_date IS the same weekday, skip it (it's the closed date we came from)."""
    mock_adapter.get_schedule.return_value = [_schedule_friday()]
    result = await provider.find_next_open(117, date(2026, 3, 6), max_weeks=4)
    assert result is not None
    assert result.date == date(2026, 3, 13)


@pytest.mark.asyncio
async def test_find_next_open_none_found(
    provider: ImpulseStickerProvider, mock_adapter: ImpulseAdapter
) -> None:
    # No matching schedule in get_schedule → no next open date.
    mock_adapter.get_schedule.return_value = []
    from_d = date(2026, 3, 2)
    result = await provider.find_next_open(117, from_d, max_weeks=4)
    assert result is None


# --- find_alternatives ---


@pytest.mark.asyncio
async def test_find_alternatives_same_branch_first(
    provider: ImpulseStickerProvider, mock_adapter: ImpulseAdapter
) -> None:
    t1 = Schedule(
        id=101,
        regular=True,
        day=5,
        minutesBegin=1170,
        group={"style": {"id": 5}, "name": "Strip"},
        branch={"id": 1, "name": "Гоголя"},
    )
    t2 = Schedule(
        id=102,
        regular=True,
        day=5,
        minutesBegin=1170,
        group={"style": {"id": 5}, "name": "Strip"},
        branch={"id": 2, "name": "Семёновская"},
    )
    mock_adapter.get_schedule.return_value = [t1, t2]
    from_d = date(2026, 3, 2)
    result = await provider.find_alternatives(style_id=5, branch_id=1, from_date=from_d)
    assert len(result) >= 1
    assert result[0].schedule_id == 101


@pytest.mark.asyncio
async def test_find_alternatives_no_open(
    provider: ImpulseStickerProvider, mock_adapter: ImpulseAdapter
) -> None:
    t1 = Schedule(
        id=101,
        regular=True,
        day=5,
        minutesBegin=1170,
        group={"style": {"id": 5}},
        branch={"id": 1},
        sticker={"name": "НЕЛЬЗЯ ПРИСОЕДИНИТЬСЯ"},
    )
    mock_adapter.get_schedule.return_value = [t1]
    from_d = date(2026, 3, 2)
    result = await provider.find_alternatives(style_id=5, branch_id=1, from_date=from_d)
    assert result == []
