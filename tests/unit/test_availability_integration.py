"""Integration tests for RFC-005 availability (engine + schedule_flow + provider).

Tests full flow with mocked CRM: closed group block, open group proceeds,
schedule markers, graceful degradation without provider.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.core.availability.impulse_provider import ImpulseStickerProvider
from app.core.availability.protocol import AvailabilityStatus, GroupAvailability
from app.core.engine import ConversationEngine
from app.core.guardrails import GuardrailRunner
from app.core.prompt_builder import PromptBuilder
from app.core.schedule_flow import generate_schedule_response
from app.knowledge.base import load_knowledge_base, StickerMapping
from app.models import ConversationState, Session, SlotValues, UnifiedMessage


# ---------------------------------------------------------------------------
# In-memory session store (same pattern as e2e)
# ---------------------------------------------------------------------------

class _InMemoryStore:
    def __init__(self):
        self._sessions: dict[tuple[str, str], Session] = {}

    def reset(self):
        self._sessions.clear()

    def get(self, channel: str, chat_id: str) -> Session | None:
        return self._sessions.get((channel, chat_id))

    def save(self, session: Session) -> None:
        self._sessions[(session.channel, session.chat_id)] = session

    def delete(self, channel: str, chat_id: str) -> None:
        self._sessions.pop((channel, chat_id), None)


_STORE = _InMemoryStore()


def _make_fake_row(session: Session):
    from datetime import timedelta
    slots_dict = session.slots.model_dump(mode="json")
    metadata = {"trace_id": str(session.trace_id)}
    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)

    class _Row(dict):
        pass

    return _Row({
        "channel": session.channel,
        "chat_id": session.chat_id,
        "fsm_state": session.state.value,
        "slots": slots_dict,
        "history": [],
        "metadata": metadata,
        "expires_at": expires_at,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
    })


async def _fake_fetchrow(query: str, *args):
    if "FROM sessions" in query and len(args) >= 2:
        session = _STORE.get(str(args[0]), str(args[1]))
        if session is None:
            return None
        return _make_fake_row(session)
    return None


async def _fake_execute(query: str, *args):
    if "INTO idempotency_locks" in query:
        return "INSERT 1"
    if "FROM idempotency_locks" in query or "DELETE FROM idempotency_locks" in query:
        return "OK"
    if "INTO sessions" in query and len(args) >= 3:
        channel, chat_id = str(args[0]), str(args[1])
        existing = _STORE.get(channel, chat_id)
        if existing:
            existing.state = ConversationState(args[2])
            existing.slots = SlotValues.model_validate(args[3])
            existing.updated_at = args[8]
        else:
            from uuid import UUID
            meta = args[5] or {}
            tid = UUID(meta.get("trace_id", str(uuid4()))) if meta.get("trace_id") else uuid4()
            session = Session(
                trace_id=tid,
                channel=channel,
                chat_id=chat_id,
                state=ConversationState(args[2]),
                slots=SlotValues.model_validate(args[3]),
                created_at=args[7],
                updated_at=args[8],
                expires_at=args[6],
            )
            _STORE.save(session)
        return "INSERT 1"
    if "DELETE FROM sessions" in query and len(args) >= 2:
        _STORE.delete(str(args[0]), str(args[1]))
        return "DELETE 1"
    return "OK"


async def _fake_fetch(query: str, *args):
    return []


# ---------------------------------------------------------------------------
# Mock availability provider — configurable per (schedule_id, date)
# ---------------------------------------------------------------------------

class _MockAvailabilityProvider:
    """Returns CLOSED/OPEN per (schedule_id, date) for testing."""

    def __init__(self, closed_keys: set[tuple[int, str]] | None = None):
        # closed_keys: set of (schedule_id, date.isoformat())
        self._closed = closed_keys or set()

    async def get_availability(self, schedule_id: int, target_date) -> GroupAvailability:
        key = (schedule_id, target_date.isoformat())
        status = AvailabilityStatus.CLOSED if key in self._closed else AvailabilityStatus.OPEN
        return GroupAvailability(schedule_id=schedule_id, date=target_date, status=status)

    async def find_next_open(self, schedule_id: int, from_date, max_weeks: int = 4) -> GroupAvailability | None:
        from datetime import timedelta
        # Return next Friday if from_date is Friday
        for w in range(max_weeks):
            for d in range(1, 8):
                cand = from_date + timedelta(days=d + w * 7)
                key = (schedule_id, cand.isoformat())
                if key not in self._closed:
                    return GroupAvailability(schedule_id=schedule_id, date=cand, status=AvailabilityStatus.OPEN)
        return None

    async def find_alternatives(self, style_id: int, branch_id, from_date, teacher_id=None) -> list[GroupAvailability]:
        return []


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def kb():
    return load_knowledge_base("knowledge/studio.yaml")


@pytest.fixture(autouse=True)
def patch_postgres():
    _STORE.reset()
    with patch("app.storage.postgres.PostgresStorage.fetchrow", side_effect=_fake_fetchrow), \
         patch("app.storage.postgres.PostgresStorage.execute", side_effect=_fake_execute), \
         patch("app.storage.postgres.PostgresStorage.fetch", side_effect=_fake_fetch):
        yield _STORE


@pytest.fixture
def mock_impulse():
    adapter = MagicMock()
    future_dt = datetime.now(timezone.utc) + timedelta(days=7)
    sch = MagicMock()
    sch.id = 117
    sch.day = future_dt.weekday() + 1  # Impulse: 1=Mon
    sch.minutes_begin = 19 * 60
    sch.style_name = "Frame Up Strip"
    sch.teacher_name = "Настя"
    sch.group = {"style": {"id": 1, "name": "Frame Up Strip"}}
    sch.branch = {"name": "Тест"}
    adapter.get_schedule = AsyncMock(return_value=[sch])
    adapter.health_check = AsyncMock(return_value=True)
    client = MagicMock()
    client.id = 101
    client.name = "Тест"
    client.phone_str = "89241234567"
    adapter.find_client = AsyncMock(return_value=None)
    adapter.create_client = AsyncMock(return_value=client)
    reservation = MagicMock()
    reservation.id = 999
    adapter.create_booking = AsyncMock(return_value=reservation)
    adapter.list_bookings = AsyncMock(return_value=[])
    return adapter


@pytest.fixture
def mock_llm():
    router = MagicMock()
    router.call = AsyncMock()
    return router


def _make_message(text: str, chat_id: str = "avail_int") -> UnifiedMessage:
    return UnifiedMessage(
        channel="telegram",
        chat_id=chat_id,
        message_id=str(uuid4()),
        timestamp=datetime.now(timezone.utc),
        text=text,
        message_type="text",
        sender_phone=None,
        sender_name="Test",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_booking_blocked_for_closed_group(kb, mock_impulse, mock_llm, patch_postgres):
    """Closed group → no create_booking, response offers alternative."""
    future_dt = (datetime.now(timezone.utc) + timedelta(days=7)).replace(hour=19, minute=0, second=0, microsecond=0)
    closed_provider = _MockAvailabilityProvider(closed_keys={(117, future_dt.date().isoformat())})

    engine = ConversationEngine(
        llm_router=mock_llm,
        prompt_builder=PromptBuilder(kb),
        guardrails=GuardrailRunner(kb),
        impulse_adapter=mock_impulse,
        kb=kb,
        availability_provider=closed_provider,
    )

    from app.core.conversation import get_or_create_session, update_slots
    from app.storage.session_store import delete_session

    await delete_session("telegram", "avail_int")
    session = await get_or_create_session(str(uuid4()), "telegram", "avail_int")
    await update_slots(
        session,
        group="Frame Up Strip",
        branch="Тест",
        datetime_resolved=future_dt,
        client_name="Маша",
        client_phone="89241234567",
        schedule_id="117",
        confirmed=True,
    )

    resp = await engine.handle_message(_make_message("да", "avail_int"), uuid4())

    assert "закрыт" in resp.lower() or "ближайшая" in resp.lower()
    assert "записала" not in resp.lower() and "подтверждаю" not in resp.lower()
    mock_impulse.create_booking.assert_not_called()


@pytest.mark.asyncio
async def test_booking_proceeds_for_open_group(kb, mock_impulse, mock_llm, patch_postgres):
    """Open group (no stickers) → create_booking is called."""
    open_provider = _MockAvailabilityProvider(closed_keys=set())

    engine = ConversationEngine(
        llm_router=mock_llm,
        prompt_builder=PromptBuilder(kb),
        guardrails=GuardrailRunner(kb),
        impulse_adapter=mock_impulse,
        kb=kb,
        availability_provider=open_provider,
    )

    from app.core.conversation import get_or_create_session, update_slots
    from app.storage.session_store import delete_session

    await delete_session("telegram", "avail_int")
    future_dt = (datetime.now(timezone.utc) + timedelta(days=7)).replace(hour=19, minute=0, second=0, microsecond=0)
    session = await get_or_create_session(str(uuid4()), "telegram", "avail_int")
    await update_slots(
        session,
        group="Frame Up Strip",
        branch="Тест",
        datetime_resolved=future_dt,
        client_name="Маша",
        client_phone="89241234567",
        schedule_id="117",
        confirmed=True,
    )

    await engine.handle_message(_make_message("да", "avail_int"), uuid4())

    mock_impulse.create_booking.assert_called_once()


@pytest.mark.asyncio
async def test_schedule_display_includes_markers(kb, mock_impulse):
    """Schedule with mixed stickers → response contains ✅ and ❌."""
    sticker_config = StickerMapping(
        closed_keywords=["ЗАКРЫТО"],
        open_keywords=["МОЖНО"],
        priority_keywords=[],
        holiday_keywords=[],
        info_keywords=[],
        unknown_action="open",
    )
    mock_impulse.get_additions = AsyncMock(side_effect=lambda d: [
        {"schedule": {"id": 117}, "name": "МОЖНО ПРИСОЕДИНИТЬСЯ"} if d.day % 2 == 0 else {"schedule": {"id": 117}, "name": "ЗАКРЫТО"},
    ])
    sch = MagicMock()
    sch.id = 117
    sch.day = 5  # Impulse Friday
    sch.minutes_begin = 1170
    sch.minutes_end = 1230
    sch.style_name = "High Heels"
    sch.teacher_name = "X"
    sch.branch_name = "Тест"
    sch.group = {"style": {"id": 1}}
    sch.branch = {"name": "Тест"}
    sch.date_begin = None
    sch.date_end = None
    sch.regular = True
    mock_impulse.get_schedule = AsyncMock(return_value=[sch])
    provider = ImpulseStickerProvider(adapter=mock_impulse, config=sticker_config)

    slots = {"group": "High Heels", "branch": "Тест"}
    text, _, _ = await generate_schedule_response(
        mock_impulse, slots, uuid4(), availability_provider=provider
    )

    assert "✅" in text
    assert "❌" in text


@pytest.mark.asyncio
async def test_graceful_degradation_no_provider(kb, mock_impulse, mock_llm, patch_postgres):
    """Engine without availability provider → booking proceeds normally."""
    engine = ConversationEngine(
        llm_router=mock_llm,
        prompt_builder=PromptBuilder(kb),
        guardrails=GuardrailRunner(kb),
        impulse_adapter=mock_impulse,
        kb=kb,
        availability_provider=None,
    )

    from app.core.conversation import get_or_create_session, update_slots
    from app.storage.session_store import delete_session

    await delete_session("telegram", "avail_int")
    future_dt = (datetime.now(timezone.utc) + timedelta(days=7)).replace(hour=19, minute=0, second=0, microsecond=0)
    session = await get_or_create_session(str(uuid4()), "telegram", "avail_int")
    await update_slots(
        session,
        group="Frame Up Strip",
        branch="Тест",
        datetime_resolved=future_dt,
        client_name="Маша",
        client_phone="89241234567",
        schedule_id="117",
        confirmed=True,
    )

    await engine.handle_message(_make_message("да", "avail_int"), uuid4())

    mock_impulse.create_booking.assert_called_once()
