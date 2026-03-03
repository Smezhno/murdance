"""ImpulseStickerProvider — GroupAvailabilityProvider for Impulse CRM stickers (RFC-005 §5.3)."""

from datetime import date, timedelta

import structlog

from app.core.availability.classifier import resolve_multiple_stickers
from app.core.availability.protocol import AvailabilityStatus, GroupAvailability
from app.core.availability.schedule_expander import expand_schedule
from app.integrations.impulse.adapter import ImpulseAdapter
from app.integrations.impulse.models import impulse_day_to_weekday
from app.knowledge.base import StickerMapping

logger = structlog.get_logger(__name__)


class ImpulseStickerProvider:
    """Implements GroupAvailabilityProvider for Impulse CRM stickers."""

    def __init__(self, adapter: ImpulseAdapter, config: StickerMapping) -> None:
        self._adapter = adapter
        self._config = config

    async def get_availability(self, schedule_id: int, target_date: date) -> GroupAvailability:
        """Check availability from schedule entry sticker (not schedule/addition).

        schedule/addition endpoint requires session auth and fails with Basic Auth (HTTP 500).
        Instead, read sticker from the schedule entry returned by schedule/list.
        Schedule is cached for 15 min — no extra API calls.
        """
        schedules = await self._adapter.get_schedule()
        try:
            sch = next((s for s in schedules if s.id == schedule_id), None)

            if sch is None:
                return GroupAvailability(
                    schedule_id=schedule_id,
                    date=target_date,
                    status=AvailabilityStatus.OPEN,
                )

            sticker_text = sch.sticker_name
            if not sticker_text:
                return GroupAvailability(
                    schedule_id=schedule_id,
                    date=target_date,
                    status=AvailabilityStatus.OPEN,
                )

            status = self._classify_sticker(sticker_text)
            return GroupAvailability(
                schedule_id=schedule_id,
                date=target_date,
                status=status,
                sticker_text=sticker_text,
            )
        except Exception as e:
            logger.warning(
                "sticker_check_error",
                schedule_id=schedule_id,
                date=target_date.isoformat(),
                error=str(e),
            )
            return GroupAvailability(
                schedule_id=schedule_id,
                date=target_date,
                status=AvailabilityStatus.OPEN,
            )

    def _classify_sticker(self, text: str) -> AvailabilityStatus:
        """Classify sticker text to availability status using config keywords."""
        upper = text.upper().strip()
        for kw in self._config.closed_keywords:
            if kw.upper() in upper:
                return AvailabilityStatus.CLOSED
        for kw in self._config.open_keywords:
            if kw.upper() in upper:
                return AvailabilityStatus.OPEN
        for kw in self._config.priority_keywords:
            if kw.upper() in upper:
                return AvailabilityStatus.PRIORITY
        for kw in self._config.holiday_keywords:
            if kw.upper() in upper:
                return AvailabilityStatus.HOLIDAY
        for kw in self._config.info_keywords:
            if kw.upper() in upper:
                return AvailabilityStatus.INFO
        return (
            AvailabilityStatus.OPEN
            if self._config.unknown_action == "open"
            else AvailabilityStatus.CLOSED
        )

    async def find_next_open(
        self, schedule_id: int, from_date: date, max_weeks: int = 4
    ) -> GroupAvailability | None:
        """Next OPEN/PRIORITY date for this schedule. Resolves day of week from get_schedule()."""
        try:
            templates = await self._adapter.get_schedule()
            template = next((t for t in templates if t.id == schedule_id), None)
            if template is None or template.day is None:
                return None
            python_weekday = impulse_day_to_weekday(template.day)
            days_ahead = (python_weekday - from_date.weekday() + 7) % 7
            if days_ahead == 0:
                days_ahead = 7  # from_date is already that weekday (and closed) — start next week
            for week in range(max_weeks):
                candidate = from_date + timedelta(days=days_ahead + week * 7)
                avail = await self.get_availability(schedule_id, candidate)
                if avail.status in (AvailabilityStatus.OPEN, AvailabilityStatus.PRIORITY):
                    return avail
        except Exception as e:
            logger.warning("find_next_open_failed", schedule_id=schedule_id, error=str(e))
        return None

    async def find_alternatives(
        self,
        style_id: int,
        branch_id: int | None,
        from_date: date,
        teacher_id: int | None = None,
    ) -> list[GroupAvailability]:
        """Open alternatives for this style, same branch first then same teacher, limit 5."""
        try:
            templates = await self._adapter.get_schedule()
            style_templates = [
                t for t in templates
                if isinstance(t.group, dict) and (t.group.get("style") or {}).get("id") == style_id
            ]
            if not style_templates:
                return []
            to_date = from_date + timedelta(days=14)
            slots = expand_schedule(style_templates, from_date, to_date)
            results: list[tuple[GroupAvailability, int | None, int | None]] = []
            # TODO: batch get_additions per unique date to avoid N API calls (cache helps after first run)
            for slot in slots:
                avail = await self.get_availability(slot.schedule_id, slot.date)
                if avail.status in (AvailabilityStatus.OPEN, AvailabilityStatus.PRIORITY):
                    results.append((avail, slot.branch_id, slot.teacher_id))
            results.sort(
                key=lambda x: (
                    0 if x[1] == branch_id else 1,
                    0 if x[2] == teacher_id else 1,
                    x[0].date,
                )
            )
            return [avail for avail, _, _ in results[:5]]
        except Exception as e:
            logger.warning("find_alternatives_failed", style_id=style_id, error=str(e))
            return []
