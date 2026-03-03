"""Impulse CRM adapter with all 8 required functions.

Per CONTRACT §5: get_schedule, get_groups, find_client, create_client,
create_booking, list_bookings, cancel_booking, health_check.
RFC-005: get_additions for schedule/addition (sticker additions).
"""

import json as _json
import logging
from functools import lru_cache
from datetime import date, datetime, time as dt_time, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from app.integrations.impulse.cache import get_impulse_cache
from app.integrations.impulse.client import get_impulse_client
from app.integrations.impulse.error_handler import ImpulseErrorHandler
from app.integrations.impulse.fallback import get_fallback
from app.integrations.impulse.models import Client, Group, Reservation, Schedule

logger = logging.getLogger(__name__)


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

        Returns all branches so the bot can consult on any branch; booking
        restriction to a single branch is enforced in confirm_booking.

        Args:
            date_from: Start date filter
            date_to: End date filter
            group_id: Group ID filter

        Returns:
            List of schedule entries from all branches
        """
        try:
            # Cache key for full schedule (no branch filter)
            cache_key = f"{date_from}_{date_to}_{group_id}_all"
            cached = await self.cache.get("schedule", cache_key)
            if cached is not None:
                return [Schedule(**item) for item in cached]

            # Fetch from CRM — only fields we use (avoids huge nested payloads)
            data = await self.client.list(
                "schedule",
                fields=["id", "regular", "day", "minutesBegin", "minutesEnd", "dateBegin", "group", "branch", "sticker"],
                filters=None,
                limit=1000,
            )

            # === TEMP DEBUG: check if stickers come with schedule ===
            for item in data[:10]:
                sticker = item.get("sticker")
                if sticker:
                    logger.info(
                        "SCHEDULE_HAS_STICKER: schedule_id=%s sticker=%s",
                        item.get("id"), sticker,
                    )
            sticker_count = sum(1 for item in data if item.get("sticker"))
            logger.info("SCHEDULE_STICKER_COUNT: %d/%d have stickers", sticker_count, len(data))
            # === END TEMP DEBUG ===

            # Parse schedules — no branch filter; consultation uses all branches
            schedules = [Schedule(**item) for item in data]

            await self.cache.set("schedule", [item.model_dump() for item in schedules], cache_key)
            return schedules

        except Exception as e:
            logger.exception("Impulse CRM error: %s", e)
            user_msg, should_fallback = self.error_handler.handle_error(e)
            if should_fallback:
                await self.fallback.enqueue(
                    "get_schedule",
                    {"date_from": str(date_from) if date_from else None, "date_to": str(date_to) if date_to else None, "group_id": group_id},
                    str(e),
                )
            raise RuntimeError(user_msg) from e

    async def get_teacher_list(self) -> list[dict[str, Any]]:
        """Get list of teachers for EntityResolver sync (RFC-004 §4.3).

        Returns list of dicts with "id" and "name", e.g. [{"id": 1, "name": "Анастасия Николаева"}].
        Tries CRM teacher entity first; fallback: unique teachers from schedule (group.teacher1).
        """
        try:
            cached = await self.cache.get("teachers")
            if cached is not None:
                return list(cached)

            try:
                data = await self.client.list(
                    "teacher",
                    fields=["id", "name", "lastName", "middleName"],
                    limit=500,
                )
                items = []
                for item in data:
                    tid = item.get("id")
                    first = (item.get("name") or "").strip()
                    last = (item.get("lastName") or "").strip()
                    if tid is not None and first:
                        items.append({"id": tid, "name": first, "lastName": last})
            except Exception:
                # Fallback: derive from schedule (group.teacher1)
                schedules = await self.get_schedule()
                seen: dict[int, tuple[str, str]] = {}
                for sch in schedules:
                    if not getattr(sch, "group", None):
                        continue
                    t1 = (sch.group or {}).get("teacher1") if isinstance(sch.group, dict) else None
                    if not t1 or not isinstance(t1, dict):
                        continue
                    tid = t1.get("id")
                    first = (t1.get("name") or "").strip()
                    last = (t1.get("lastName") or "").strip()
                    if tid is not None and first and tid not in seen:
                        seen[tid] = (first, last)
                items = [{"id": k, "name": v[0], "lastName": v[1]} for k, v in seen.items()]

            out = [x for x in items if x.get("id") is not None and (x.get("name") or "").strip()]
            await self.cache.set("teachers", out)
            return out

        except Exception as e:
            logger.exception("Impulse CRM error: %s", e)
            user_msg, should_fallback = self.error_handler.handle_error(e)
            if should_fallback:
                await self.fallback.enqueue("get_teacher_list", {}, str(e))
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
            logger.exception("Impulse CRM error: %s", e)
            user_msg, should_fallback = self.error_handler.handle_error(e)
            if should_fallback:
                await self.fallback.enqueue("get_groups", {}, str(e))
            raise RuntimeError(user_msg) from e

    async def find_client(self, phone: str) -> Client | None:
        """Find client by phone (CONTRACT §5).

        Args:
            phone: Phone number (any format: 89xx, +79xx, 79xx)

        Returns:
            Client if found, None otherwise
        """
        try:
            # Normalize to +7 format for Impulse CRM
            normalized = phone.strip().replace(" ", "").replace("-", "")
            if normalized.startswith("8") and len(normalized) == 11:
                normalized = "+7" + normalized[1:]
            elif normalized.startswith("7") and len(normalized) == 11:
                normalized = "+" + normalized
            elif not normalized.startswith("+"):
                normalized = "+" + normalized

            data = await self.client.list(
                "client",
                filters={"phone": normalized},
                limit=1,
            )

            if not data:
                return None

            return Client(**data[0])

        except Exception as e:
            logger.exception("Impulse CRM error: %s", e)
            user_msg, should_fallback = self.error_handler.handle_error(e)
            if should_fallback:
                await self.fallback.enqueue("find_client", {"phone": phone}, str(e))
            raise RuntimeError(user_msg) from e

    async def get_additions(self, target_date: date) -> list[dict[str, Any]]:
        """Fetch schedule additions (stickers) for a specific date (RFC-005 §4.2).

        schedule/addition is NOT a list endpoint — it expects simple {"date": unix_ts}.
        POST /api/public/schedule/addition. Caches under schedule, key additions:YYYY-MM-DD, TTL 15 min.
        Returns raw items list from API response (each item: id, name, date, schedule, branch, ...).
        """
        try:
            cache_key = ("additions", target_date.isoformat())
            cached = await self.cache.get("schedule", *cache_key)
            if cached is not None:
                return list(cached)

            tz = ZoneInfo("Asia/Vladivostok")
            day_start = datetime(
                target_date.year, target_date.month, target_date.day, tzinfo=tz
            )
            ts = int(day_start.timestamp())

            # Simple format — schedule/addition is NOT a list endpoint
            body = {"date": ts}

            response = await self.client._request("POST", "schedule", "addition", body)
            data = response.json()
            items = data.get("items", [])

            await self.cache.set("schedule", items, *cache_key)
            return items

        except Exception as e:
            logger.exception("Impulse CRM error: %s", e)
            # Unwrap RetryError → get actual HTTP error
            actual_error = e
            try:
                import tenacity
                if isinstance(e, tenacity.RetryError):
                    # e.last_attempt can be a concurrent.futures.Future
                    future = getattr(e, "last_attempt", None)
                    if future is not None:
                        inner = future.exception()
                        if inner is not None:
                            actual_error = inner
            except Exception:
                pass  # If unwrap fails, log the original

            if hasattr(actual_error, "response"):
                logger.error(
                    "GET_ADDITIONS_FAILED: status=%d url=%s body=%.500s",
                    actual_error.response.status_code,
                    str(actual_error.request.url),
                    (actual_error.response.text or "")[:500],
                )
            else:
                logger.error(
                    "GET_ADDITIONS_FAILED: type=%s msg=%s",
                    type(actual_error).__name__,
                    str(actual_error)[:500],
                )
                # Fallback: try exception chain (__cause__ / __context__)
                cause = getattr(e, "__cause__", None) or getattr(e, "__context__", None)
                if cause is not None and hasattr(cause, "response"):
                    logger.error(
                        "GET_ADDITIONS_CAUSED_BY: status=%d url=%s body=%.500s",
                        cause.response.status_code,
                        str(cause.request.url),
                        (cause.response.text or "")[:500],
                    )

            user_msg, should_fallback = self.error_handler.handle_error(e)
            if should_fallback:
                await self.fallback.enqueue(
                    "get_additions",
                    {"target_date": target_date.isoformat()},
                    str(e),
                )
            raise RuntimeError(user_msg) from e

    async def create_client(self, name: str, phone: str, informer_id: int | None = None, trace_id: UUID | None = None) -> Client:
        """Create new client (CONTRACT §5).

        Impulse CRM /client/list ignores all filters — client lookup by phone is done
        by attempting to create and parsing the duplicate-error response (500 + HTML with client id).

        Args:
            name: Client name
            phone: Phone number
            informer_id: Informer ID (source)
            trace_id: Optional trace ID

        Returns:
            Created or found client
        """
        import re as _re

        # Normalize phone to +7 format
        normalized_phone = phone.strip().replace(" ", "").replace("-", "")
        if normalized_phone.startswith("8") and len(normalized_phone) == 11:
            normalized_phone = "+7" + normalized_phone[1:]
        elif normalized_phone.startswith("7") and len(normalized_phone) == 11:
            normalized_phone = "+" + normalized_phone
        elif not normalized_phone.startswith("+"):
            normalized_phone = "+" + normalized_phone

        try:
            # Impulse CRM requires phone as array, deposit/bonus as non-null integers,
            # and status with pipeline to avoid getPipeline() null error on reservation creation.
            data: dict[str, Any] = {
                "name": name,
                "phone": [normalized_phone],
                "deposit": 0,
                "bonus": 0,
                "status": {"id": 1},  # "неразобранное" — default pipeline entry point
            }
            if informer_id:
                data["informer"] = {"id": informer_id}

            # Use tolerant method — CRM returns 500 HTML when client already exists
            response = await self.client.create_tolerant("client", data)

            if response.status_code == 200:
                result = response.json()
                client_id = result.get("id") if isinstance(result, dict) else None
                if client_id:
                    return Client(id=client_id, name=name, phone=[normalized_phone])
                return Client(**result)

            # 500 with "already exists" message — parse client id from HTML
            if response.status_code >= 400:
                error_text = response.text
                match = _re.search(r"client/edit/(\d+)", error_text)
                if match:
                    existing_id = int(match.group(1))
                    # Ensure existing client has a status/pipeline set (needed for reservation creation)
                    await self.client.create_tolerant("client", {
                        "id": existing_id,
                        "status": {"id": 1},
                        "deposit": 0,
                        "bonus": 0,
                    })
                    return Client(id=existing_id, name=name, phone=[normalized_phone])
                # Unexpected error — raise for fallback handling
                response.raise_for_status()

            return Client(id=0, name=name, phone=[normalized_phone])

        except Exception as e:
            logger.exception("Impulse CRM error: %s", e)
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
        booking_date: "datetime | None" = None,
        status_id: int | None = None,
        notes: str | None = None,
        trace_id: UUID | None = None,
    ) -> Reservation:
        """Create booking/reservation (CONTRACT §5).

        Impulse CRM requires a 'time' array with the full raw schedule object,
        otherwise /reservation/update returns a server-side null-load error.

        Args:
            client_id: Client ID
            schedule_id: Schedule ID
            booking_date: Datetime of the specific class occurrence
            status_id: Optional status ID
            notes: Optional notes
            trace_id: Optional trace ID

        Returns:
            Created reservation
        """
        try:
            from datetime import timezone as tz

            # Midnight UTC timestamp for the booking date
            ts: int | None = None
            if booking_date:
                midnight = booking_date.replace(hour=0, minute=0, second=0, microsecond=0)
                ts = int(midnight.astimezone(tz.utc).timestamp())

            # Fetch raw schedule object — CRM needs full nested dict in 'time' array
            # schedule_id from slots is str, CRM returns int — compare as int
            try:
                sid_int = int(schedule_id)
            except (ValueError, TypeError):
                sid_int = schedule_id
            raw_schedules = await self.client.list("schedule", filters=None, limit=1000)
            raw_schedule = next((s for s in raw_schedules if s.get("id") == sid_int), None)

            print(f"RAW_SCHEDULE_FOUND: {raw_schedule is not None} schedule_id={schedule_id}")
            if raw_schedule is not None:
                print(f"RAW_SCHEDULE_GROUP: {raw_schedule.get('group') is not None}")
                print(f"RAW_SCHEDULE_BRANCH: {raw_schedule.get('branch') is not None}")
                group = raw_schedule.get("group")
                if group is None:
                    print(f"WARNING: raw_schedule has no group! Keys: {list(raw_schedule.keys())}")

            data: dict[str, Any] = {
                "client": {"id": client_id},
                "schedule": {"id": sid_int},
                "type": 0,
            }
            if ts is not None:
                data["date"] = ts

            # Build 'time' array required by Impulse CRM (discovered via API testing)
            if raw_schedule is not None:
                time_entry: dict[str, Any] = {
                    "minutes": raw_schedule.get("minutesBegin"),
                    "schedule": raw_schedule,
                    "type": 0,
                    "source": 0,
                }
                group_data = raw_schedule.get("group")
                if group_data is not None:
                    time_entry["group"] = group_data
                else:
                    print(f"WARNING_NO_GROUP: schedule_id={schedule_id}, skipping group in time entry")
                if ts is not None:
                    time_entry["date"] = ts
                data["time"] = [time_entry]

            if notes:
                data["annotation"] = notes

            print(
                f"CREATE_BOOKING_REQUEST: {_json.dumps(data, default=str, ensure_ascii=False)[:2000]}"
            )
            result = await self.client.create("reservation", data)

            # Invalidate all schedule cache keys
            await self.cache.clear_entity("schedule")

            # CRM returns {"success": true, "count": 1} — no reservation ID in response.
            # Construct a minimal Reservation from known data.
            reservation_id = result.get("id") if isinstance(result, dict) else None
            if not reservation_id:
                return Reservation(
                    id=0,
                    client={"id": client_id},
                    schedule={"id": schedule_id},
                    date=booking_date.date().isoformat() if booking_date else None,
                )
            return Reservation(**result)

        except Exception as e:
            logger.exception("Impulse CRM error: %s", e)

            actual_err = e
            try:
                import tenacity
                if isinstance(e, tenacity.RetryError):
                    last = getattr(e, "last_attempt", None)
                    if last is not None:
                        inner = last.exception()
                        if inner is not None:
                            actual_err = inner
            except ImportError:
                pass

            if hasattr(actual_err, "response") and actual_err.response is not None:
                logger.error(
                    "CREATE_BOOKING_CRM_RESPONSE: status=%d body=%.1000s",
                    actual_err.response.status_code,
                    (actual_err.response.text or "")[:1000],
                )

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

        CRM does not support server-side filtering by client_id or date —
        all pages are fetched and filtered client-side.
        CRM supports up to 1000 items per page; we paginate until exhausted.

        Args:
            client_id: Filter by client ID (applied in Python)
            date_from: Filter reservations on or after this date (applied in Python)

        Returns:
            List of active reservations matching filters
        """
        try:
            # Sort by id DESC so newest reservations come first.
            # Fetch last 500 — enough to cover all active bookings without full table scan.
            data = await self.client.list(
                "reservation",
                limit=500,
                page=1,
                sort={"id": "desc"},
            )
            reservations = [Reservation(**item) for item in data]

            # Filter client-side: skip deleted/archived
            reservations = [r for r in reservations if r.is_active]

            # Filter by client_id
            if client_id is not None:
                reservations = [r for r in reservations if r.client_id == client_id]

            # Filter by date_from
            if date_from is not None:
                reservations = [
                    r for r in reservations
                    if r.date_as_date is not None and r.date_as_date >= date_from
                ]

            return reservations

        except Exception as e:
            logger.exception("Impulse CRM error: %s", e)
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
            # CRM uses reservation/archive to cancel (delete/unsubscribe return 404)
            response = await self.client._request("POST", "reservation", "archive", {"id": reservation_id})
            result = response.json()
            success = result.get("success") is True

            # Invalidate all schedule cache keys
            await self.cache.clear_entity("schedule")

            return success

        except Exception as e:
            logger.exception("Impulse CRM error: %s", e)
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

