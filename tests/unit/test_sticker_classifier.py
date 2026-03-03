"""Unit tests for sticker classifier (RFC-005 §5.3.2, §5.3.3)."""

from datetime import date

import pytest

from app.core.availability.classifier import classify_sticker, resolve_multiple_stickers
from app.core.availability.protocol import AvailabilityStatus, GroupAvailability
from app.knowledge.base import StickerMapping


@pytest.fixture
def sticker_config() -> StickerMapping:
    """Default StickerMapping matching studio.yaml + ЗАКРЫТ for substring test."""
    return StickerMapping(
        open_keywords=["МОЖНО ПРИСОЕДИНИТЬСЯ"],
        closed_keywords=["НЕЛЬЗЯ ПРИСОЕДИНИТЬСЯ", "НЕЛЬЗЬЯ ПРИСОЕДИНИТЬСЯ", "ЗАКРЫТО", "ЗАКРЫТ"],
        priority_keywords=["НОВАЯ ХОРЕОГРАФИЯ"],
        holiday_keywords=["ВЫХОДНОЙ"],
        info_keywords=["СТАРТ", "ОТКРЫТЫЙ УРОК"],
        unknown_action="open",
    )


@pytest.fixture
def sticker_config_closed_default() -> StickerMapping:
    """StickerMapping with unknown_action='closed'."""
    return StickerMapping(
        open_keywords=["МОЖНО ПРИСОЕДИНИТЬСЯ"],
        closed_keywords=["НЕЛЬЗЯ ПРИСОЕДИНИТЬСЯ"],
        priority_keywords=[],
        holiday_keywords=[],
        info_keywords=[],
        unknown_action="closed",
    )


def test_exact_open(sticker_config: StickerMapping) -> None:
    assert classify_sticker("МОЖНО ПРИСОЕДИНИТЬСЯ", sticker_config) == AvailabilityStatus.OPEN


def test_exact_closed(sticker_config: StickerMapping) -> None:
    assert classify_sticker("НЕЛЬЗЯ ПРИСОЕДИНИТЬСЯ", sticker_config) == AvailabilityStatus.CLOSED


def test_typo_closed(sticker_config: StickerMapping) -> None:
    assert classify_sticker("НЕЛЬЗЬЯ ПРИСОЕДИНИТЬСЯ", sticker_config) == AvailabilityStatus.CLOSED


def test_substring_closed(sticker_config: StickerMapping) -> None:
    assert classify_sticker("НАБОР ЗАКРЫТ", sticker_config) == AvailabilityStatus.CLOSED


def test_new_choreo(sticker_config: StickerMapping) -> None:
    assert classify_sticker("НОВАЯ ХОРЕОГРАФИЯ", sticker_config) == AvailabilityStatus.PRIORITY


def test_holiday(sticker_config: StickerMapping) -> None:
    assert classify_sticker("ВЫХОДНОЙ", sticker_config) == AvailabilityStatus.HOLIDAY


def test_info_start(sticker_config: StickerMapping) -> None:
    assert classify_sticker("СТАРТ АКВАКУРСА", sticker_config) == AvailabilityStatus.INFO


def test_unknown_default_open(sticker_config: StickerMapping) -> None:
    assert classify_sticker("КАКОЙ-ТО НОВЫЙ ТЕКСТ", sticker_config) == AvailabilityStatus.OPEN


def test_unknown_action_closed(sticker_config_closed_default: StickerMapping) -> None:
    assert classify_sticker("КАКОЙ-ТО НОВЫЙ ТЕКСТ", sticker_config_closed_default) == AvailabilityStatus.CLOSED


def test_case_insensitive(sticker_config: StickerMapping) -> None:
    assert classify_sticker("можно присоединиться", sticker_config) == AvailabilityStatus.OPEN


def test_closed_beats_open(sticker_config: StickerMapping) -> None:
    stickers = [
        {"name": "МОЖНО ПРИСОЕДИНИТЬСЯ"},
        {"name": "НЕЛЬЗЯ ПРИСОЕДИНИТЬСЯ"},
    ]
    result = resolve_multiple_stickers(117, date(2026, 3, 7), stickers, sticker_config)
    assert result.status == AvailabilityStatus.CLOSED
    assert result.sticker_text == "НЕЛЬЗЯ ПРИСОЕДИНИТЬСЯ"


def test_priority_when_no_closed(sticker_config: StickerMapping) -> None:
    stickers = [
        {"name": "НОВАЯ ХОРЕОГРАФИЯ"},
        {"name": "МОЖНО ПРИСОЕДИНИТЬСЯ"},
    ]
    result = resolve_multiple_stickers(117, date(2026, 3, 7), stickers, sticker_config)
    assert result.status == AvailabilityStatus.PRIORITY
    assert result.sticker_text == "НОВАЯ ХОРЕОГРАФИЯ"


def test_all_info_returns_open(sticker_config: StickerMapping) -> None:
    stickers = [{"name": "СТАРТ АКВАКУРСА"}]
    result = resolve_multiple_stickers(117, date(2026, 3, 7), stickers, sticker_config)
    assert result.status == AvailabilityStatus.OPEN
    assert result.note == "info_only"
    assert result.sticker_text == "СТАРТ АКВАКУРСА"


def test_empty_stickers(sticker_config: StickerMapping) -> None:
    result = resolve_multiple_stickers(117, date(2026, 3, 7), [], sticker_config)
    assert result.status == AvailabilityStatus.OPEN
    assert result.schedule_id == 117
    assert result.date == date(2026, 3, 7)
