"""Sticker classifier — maps CRM sticker names to AvailabilityStatus (RFC-005 §5.3.2, §5.3.3)."""

from datetime import date

import structlog

from app.core.availability.protocol import AvailabilityStatus, GroupAvailability
from app.knowledge.base import StickerMapping

logger = structlog.get_logger(__name__)

_PRIORITY_ORDER = (
    AvailabilityStatus.CLOSED,
    AvailabilityStatus.HOLIDAY,
    AvailabilityStatus.PRIORITY,
    AvailabilityStatus.OPEN,
    AvailabilityStatus.INFO,
)


def classify_sticker(name: str, config: StickerMapping) -> AvailabilityStatus:
    """Classify sticker name by keyword match. Order: CLOSED, HOLIDAY, OPEN, PRIORITY, INFO (safety-first)."""
    name_upper = name.strip().upper()
    for kw in config.closed_keywords:
        if kw.upper() in name_upper:
            return AvailabilityStatus.CLOSED
    for kw in config.holiday_keywords:
        if kw.upper() in name_upper:
            return AvailabilityStatus.HOLIDAY
    for kw in config.open_keywords:
        if kw.upper() in name_upper:
            return AvailabilityStatus.OPEN
    for kw in config.priority_keywords:
        if kw.upper() in name_upper:
            return AvailabilityStatus.PRIORITY
    for kw in config.info_keywords:
        if kw.upper() in name_upper:
            return AvailabilityStatus.INFO
    logger.warning("unknown_sticker", name=name)
    return AvailabilityStatus.OPEN if config.unknown_action == "open" else AvailabilityStatus.CLOSED


def resolve_multiple_stickers(
    schedule_id: int,
    target_date: date,
    stickers: list[dict],
    config: StickerMapping,
) -> GroupAvailability:
    """Resolve multiple stickers on same date. Priority: CLOSED/HOLIDAY > PRIORITY > OPEN. All INFO → OPEN + note."""
    if not stickers:
        return GroupAvailability(schedule_id=schedule_id, date=target_date, status=AvailabilityStatus.OPEN)
    classified = [(classify_sticker(s.get("name", ""), config), s.get("name")) for s in stickers]
    for status in _PRIORITY_ORDER:
        for st, name in classified:
            if st == status:
                if status == AvailabilityStatus.INFO and all(c[0] == AvailabilityStatus.INFO for c in classified):
                    return GroupAvailability(
                        schedule_id=schedule_id,
                        date=target_date,
                        status=AvailabilityStatus.OPEN,
                        sticker_text=name,
                        note="info_only",
                    )
                return GroupAvailability(schedule_id=schedule_id, date=target_date, status=status, sticker_text=name)
    return GroupAvailability(schedule_id=schedule_id, date=target_date, status=AvailabilityStatus.OPEN)
