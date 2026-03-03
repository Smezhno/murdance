"""Group availability protocol — CRM-agnostic interface (RFC-005 §5.1, §5.2)."""

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Protocol


class AvailabilityStatus(Enum):
    OPEN = "open"
    CLOSED = "closed"
    PRIORITY = "priority"
    INFO = "info"
    HOLIDAY = "holiday"


@dataclass
class GroupAvailability:
    schedule_id: int
    date: date
    status: AvailabilityStatus
    sticker_text: str | None = None
    note: str | None = None


class GroupAvailabilityProvider(Protocol):
    async def get_availability(
        self, schedule_id: int, target_date: date
    ) -> GroupAvailability:
        """Доступность конкретного занятия на конкретную дату."""
        ...

    async def find_next_open(
        self, schedule_id: int, from_date: date, max_weeks: int = 4
    ) -> GroupAvailability | None:
        """Ближайшая открытая дата для этого schedule."""
        ...

    async def find_alternatives(
        self,
        style_id: int,
        branch_id: int | None,
        from_date: date,
        teacher_id: int | None = None,
    ) -> list[GroupAvailability]:
        """Альтернативные открытые группы того же направления."""
        ...
