"""Impulse CRM adapter with all 8 required functions.

Per CONTRACT §5: get_schedule, get_groups, find_client, create_client,
create_booking, list_bookings, cancel_booking, health_check.
"""

from functools import lru_cache
from datetime import date, datetime
from typing import Any
from uuid import UUID

from app.integrations.impulse.cache import get_impulse_cache
from app.integrations.impulse.client import get_impulse_client
from app.integrations.impulse.error_handler import ImpulseErrorHandler
from app.integrations.impulse.fallback import get_fallback
from app.integrations.impulse.models import Client, Group, Reservation, Schedule


class ImpulseAdapter:
    """Impulse CRM adapter (CONTRACT §5)."""

    def __init__(self) -> None:
        """Initialize adapter."""
        self.client = get_impulse_client()
        self.cache = get_impulse_cache()
        self.error_handler = ImpulseErrorHandler()
        self.fallback = get_fallback()

    async def get_schedule(
        self,
        date_from: date | None = None,
        date_to: date | None = None,
        group_id: int | None = None,
    ) -> list[Schedule]:
        """Get schedule entries (CONTRACT §5).

        Args:
            date_from: Start date filter
            date_to: End date filter
            group_id: Group ID filter

        Returns:
            List of schedule entries
        """
        try:
            # Check cache
            cache_key = f"{date_from}_{date_to}_{group_id}"
            cached = await self.cache.get("schedule", cache_key)
            if cached is not None:
                return [Schedule(**item) for item in cached]

            # Build filters
            filters: dict[str, Any] = {}
            if date_from:
                filters["date"] = date_from.isoformat()
            if group_id:
                filters["group_id"] = group_id

            # Fetch from CRM
            data = await self.client.list(
                "schedule",
                fields=["id", "group_id", "teacher_id", "hall_id", "date", "time", "duration_minutes", "max_students", "current_students", "is_active"],
                filters=filters if filters else None,
                limit=1000,
            )

            # Parse and cache
            schedules = [Schedule(**item) for item in data]
            await self.cache.set("schedule", [item.model_dump() for item in schedules], cache_key)

            return schedules

        except Exception as e:
            user_msg, should_fallback = self.error_handler.handle_error(e)
            if should_fallback:
                await self.fallback.enqueue(
                    "get_schedule",
                    {"date_from": str(date_from) if date_from else None, "date_to": str(date_to) if date_to else None, "group_id": group_id},
                    str(e),
                )
            raise RuntimeError(user_msg) from e

    async def get_groups(self) -> list[Group]:
        """Get all groups (CONTRACT §5).

        Returns:
            List of groups
        """
        try:
            # Check cache
            cached = await self.cache.get("groups")
            if cached is not None:
                return [Group(**item) for item in cached]

            # Fetch from CRM
            data = await self.client.list(
                "group",
                fields=["id", "name", "style_id", "teacher_id", "description", "is_active"],
                limit=1000,
            )

            # Parse and cache
            groups = [Group(**item) for item in data]
            await self.cache.set("groups", [item.model_dump() for item in groups])

            return groups

        except Exception as e:
            user_msg, should_fallback = self.error_handler.handle_error(e)
            if should_fallback:
                await self.fallback.enqueue("get_groups", {}, str(e))
            raise RuntimeError(user_msg) from e

    async def find_client(self, phone: str) -> Client | None:
        """Find client by phone (CONTRACT §5).

        Args:
            phone: Phone number

        Returns:
            Client if found, None otherwise
        """
        try:
            # Search by phone
            data = await self.client.list(
                "client",
                fields=["id", "name", "phone", "email", "informer_id"],
                filters={"phone": phone},
                limit=1,
            )

            if not data:
                return None

            return Client(**data[0])

        except Exception as e:
            user_msg, should_fallback = self.error_handler.handle_error(e)
            if should_fallback:
                await self.fallback.enqueue("find_client", {"phone": phone}, str(e))
            raise RuntimeError(user_msg) from e

    async def create_client(self, name: str, phone: str, informer_id: int | None = None, trace_id: UUID | None = None) -> Client:
        """Create new client (CONTRACT §5).

        Args:
            name: Client name
            phone: Phone number
            informer_id: Informer ID (source)
            trace_id: Optional trace ID

        Returns:
            Created client
        """
        try:
            data = {
                "name": name,
                "phone": phone,
            }
            if informer_id:
                data["informer_id"] = informer_id

            result = await self.client.create("client", data)
            return Client(**result)

        except Exception as e:
            user_msg, should_fallback = self.error_handler.handle_error(e)
            if should_fallback:
                await self.fallback.enqueue(
                    "create_client",
                    {"name": name, "phone": phone, "informer_id": informer_id},
                    str(e),
                    str(trace_id) if trace_id else None,
                )
                raise RuntimeError(user_msg) from e
            raise RuntimeError(user_msg) from e

    async def create_booking(
        self,
        client_id: int,
        schedule_id: int,
        status_id: int | None = None,
        notes: str | None = None,
        trace_id: UUID | None = None,
    ) -> Reservation:
        """Create booking/reservation (CONTRACT §5).

        Args:
            client_id: Client ID
            schedule_id: Schedule ID
            status_id: Optional status ID
            notes: Optional notes
            trace_id: Optional trace ID

        Returns:
            Created reservation
        """
        try:
            data = {
                "client_id": client_id,
                "schedule_id": schedule_id,
            }
            if status_id:
                data["status_id"] = status_id
            if notes:
                data["notes"] = notes

            result = await self.client.create("reservation", data)

            # Invalidate all schedule cache keys
            await self.cache.clear_entity("schedule")

            return Reservation(**result)

        except Exception as e:
            user_msg, should_fallback = self.error_handler.handle_error(e)
            if should_fallback:
                await self.fallback.enqueue(
                    "create_booking",
                    {"client_id": client_id, "schedule_id": schedule_id, "status_id": status_id, "notes": notes},
                    str(e),
                    str(trace_id) if trace_id else None,
                )
                raise RuntimeError(user_msg) from e
            raise RuntimeError(user_msg) from e

    async def list_bookings(self, client_id: int | None = None, date_from: date | None = None) -> list[Reservation]:
        """List bookings/reservations (CONTRACT §5).

        Args:
            client_id: Optional client ID filter
            date_from: Optional start date filter

        Returns:
            List of reservations
        """
        try:
            filters: dict[str, Any] = {}
            if client_id:
                filters["client_id"] = client_id
            if date_from:
                filters["date"] = date_from.isoformat()

            data = await self.client.list(
                "reservation",
                fields=["id", "client_id", "schedule_id", "status_id", "created_at", "updated_at", "notes"],
                filters=filters if filters else None,
                limit=1000,
            )

            return [Reservation(**item) for item in data]

        except Exception as e:
            user_msg, should_fallback = self.error_handler.handle_error(e)
            if should_fallback:
                await self.fallback.enqueue(
                    "list_bookings",
                    {"client_id": client_id, "date_from": str(date_from) if date_from else None},
                    str(e),
                )
            raise RuntimeError(user_msg) from e

    async def cancel_booking(self, reservation_id: int, trace_id: UUID | None = None) -> bool:
        """Cancel booking/reservation (CONTRACT §5).

        Args:
            reservation_id: Reservation ID
            trace_id: Optional trace ID

        Returns:
            True if cancelled
        """
        try:
            result = await self.client.delete("reservation", reservation_id)

            # Invalidate all schedule cache keys
            await self.cache.clear_entity("schedule")

            return result

        except Exception as e:
            user_msg, should_fallback = self.error_handler.handle_error(e)
            if should_fallback:
                await self.fallback.enqueue(
                    "cancel_booking",
                    {"reservation_id": reservation_id},
                    str(e),
                    str(trace_id) if trace_id else None,
                )
                raise RuntimeError(user_msg) from e
            raise RuntimeError(user_msg) from e

    async def health_check(self) -> bool:
        """Check CRM health (CONTRACT §5).

        Returns:
            True if CRM is healthy
        """
        return await self.client.health_check()


@lru_cache()
def get_impulse_adapter() -> ImpulseAdapter:
    """Get Impulse adapter instance (lazy init)."""
    return ImpulseAdapter()

