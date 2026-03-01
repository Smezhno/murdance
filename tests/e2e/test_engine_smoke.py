"""E2E smoke tests for ConversationEngine (RFC-003 §3.3.3).

Uses a mock ImpulseAdapter so no real CRM calls are made.
Session store is replaced with an in-memory dict so no Postgres connection
is required — these tests validate engine logic only.

Run: python -m pytest tests/e2e/test_engine_smoke.py -v
"""

import pytest
import pytest_asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from app.core.engine import ConversationEngine
from app.core.guardrails import GuardrailRunner
from app.core.prompt_builder import PromptBuilder
from app.knowledge.base import load_knowledge_base
from app.models import Session, SlotValues, UnifiedMessage


# ---------------------------------------------------------------------------
# In-memory session store — replaces Postgres for smoke tests
# ---------------------------------------------------------------------------

class _InMemoryStore:
    """In-memory session store. Patches postgres_storage.fetchrow/execute/execute_many
    so no real DB connection is needed."""

    def __init__(self):
        # key: (channel, chat_id) → Session
        self._sessions: dict[tuple[str, str], Session] = {}

    def reset(self):
        self._sessions.clear()

    def _key(self, session: Session) -> tuple[str, str]:
        return (session.channel, session.chat_id)

    def get(self, channel: str, chat_id: str) -> Session | None:
        return self._sessions.get((channel, chat_id))

    def save(self, session: Session) -> None:
        self._sessions[(session.channel, session.chat_id)] = session

    def delete(self, channel: str, chat_id: str) -> None:
        self._sessions.pop((channel, chat_id), None)


_STORE = _InMemoryStore()


def _make_fake_row(session: Session):
    """Build a dict-like object that session_store._row_to_session can parse."""
    import json
    from datetime import timedelta, timezone

    slots_dict = session.slots.model_dump(mode="json")
    metadata = {"trace_id": str(session.trace_id)}
    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)

    class _Row(dict):
        pass

    row = _Row({
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
    return row


async def _fake_fetchrow(query: str, *args):
    """Intercept SELECT from sessions."""
    if "FROM sessions" in query and len(args) >= 2:
        channel, chat_id = str(args[0]), str(args[1])
        session = _STORE.get(channel, chat_id)
        if session is None:
            return None
        return _make_fake_row(session)
    return None


async def _fake_execute(query: str, *args):
    """Intercept INSERT/UPDATE/DELETE on sessions and idempotency_locks."""
    if "INTO idempotency_locks" in query:
        # Always treat as new booking (no duplicate)
        return "INSERT 1"
    if "FROM idempotency_locks" in query or "DELETE FROM idempotency_locks" in query:
        return "OK"
    if "INTO sessions" in query:
        # args: channel, chat_id, fsm_state, slots, history, metadata, expires_at, created_at, updated_at
        channel, chat_id = str(args[0]), str(args[1])
        existing = _STORE.get(channel, chat_id)
        if existing:
            # Update slots and state in place
            from app.models import ConversationState, SlotValues
            existing.state = ConversationState(args[2])
            existing.slots = SlotValues.model_validate(args[3])
            existing.updated_at = args[8]
        else:
            from app.models import ConversationState, Session, SlotValues
            from uuid import UUID, uuid4
            meta = args[5] or {}
            raw_tid = meta.get("trace_id")
            try:
                tid = UUID(raw_tid) if raw_tid else uuid4()
            except (ValueError, AttributeError):
                tid = uuid4()
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


@pytest.fixture(autouse=True)
def patch_postgres():
    """Patch postgres_storage methods so no real DB connection is needed."""
    _STORE.reset()
    with patch("app.storage.postgres.PostgresStorage.fetchrow", side_effect=_fake_fetchrow), \
         patch("app.storage.postgres.PostgresStorage.execute", side_effect=_fake_execute), \
         patch("app.storage.postgres.PostgresStorage.fetch", side_effect=_fake_fetch):
        yield _STORE


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def kb():
    return load_knowledge_base("knowledge/studio.yaml")


@pytest.fixture
def mock_impulse():
    """Minimal ImpulseAdapter mock for smoke tests."""
    adapter = MagicMock()

    # Schedule entry used for booking confirmation — must match future_dt (7 days from now, 19:00)
    # booking_confirm.py resolves schedule_id by matching weekday + minutes_begin + group name
    future_dt = datetime.now(timezone.utc) + timedelta(days=7)
    schedule_entry = MagicMock()
    schedule_entry.id = "42"
    schedule_entry.day = future_dt.weekday()       # weekday of future_dt
    schedule_entry.minutes_begin = 19 * 60         # 19:00 → 1140 minutes
    schedule_entry.style_name = "High Heels"
    schedule_entry.teacher_name = "Катя"
    adapter.get_schedule = AsyncMock(return_value=[schedule_entry])
    adapter.health_check = AsyncMock(return_value=True)

    # Client operations
    client = MagicMock()
    client.id = 101
    client.name = "Маша Иванова"
    client.phone_str = "89241234567"
    adapter.find_client = AsyncMock(return_value=None)
    adapter.create_client = AsyncMock(return_value=client)

    # Booking
    reservation = MagicMock()
    reservation.id = 999
    adapter.create_booking = AsyncMock(return_value=reservation)

    # Cancel flow
    adapter.list_bookings = AsyncMock(return_value=[])

    return adapter


@pytest.fixture
def mock_llm():
    """LLM router mock — returns structured JSON responses."""
    router = MagicMock()
    router.call = AsyncMock()
    return router


def _make_message(text: str, chat_id: str = "smoke_test") -> UnifiedMessage:
    return UnifiedMessage(
        channel="telegram",
        chat_id=chat_id,
        message_id=str(uuid4()),
        timestamp=datetime.now(timezone.utc),
        text=text,
        message_type="text",
        sender_phone=None,
        sender_name="Smoke Test User",
    )


def _llm_resp(message: str, intent: str = "continue", slot_updates: dict | None = None,
              tool_calls: list | None = None) -> MagicMock:
    """Build a ProviderLLMResponse-like mock."""
    import json

    def _default(obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

    r = MagicMock()
    payload = {
        "message": message,
        "intent": intent,
        "slot_updates": slot_updates or {},
        "tool_calls": tool_calls or [],
    }
    r.text = json.dumps(payload, ensure_ascii=False, default=_default)
    return r


async def _drive(engine, chat_id: str, turns: list[tuple[str, MagicMock]]) -> list[str]:
    """Send a sequence of (user_text, llm_response_mock) turns.

    Returns list of bot responses.
    """
    from app.storage.session_store import delete_session
    await delete_session("telegram", chat_id)

    responses = []
    llm_iter = iter(turns)
    for user_text, llm_mock in llm_iter:
        engine._llm.call = AsyncMock(return_value=llm_mock)
        resp = await engine.handle_message(_make_message(user_text, chat_id), uuid4())
        responses.append(resp)

    return responses


# ---------------------------------------------------------------------------
# Test 1 — Happy path booking
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_happy_path_booking(kb, mock_impulse, mock_llm):
    """Full booking flow: greeting → branch → direction → schedule → contact → confirm → receipt."""
    chat_id = "smoke_happy"

    engine = ConversationEngine(
        llm_router=mock_llm,
        prompt_builder=PromptBuilder(kb),
        guardrails=GuardrailRunner(kb),
        impulse_adapter=mock_impulse,
        kb=kb,
    )

    responses = []

    async def send(text: str, llm_response: MagicMock) -> str:
        mock_llm.call = AsyncMock(return_value=llm_response)
        r = await engine.handle_message(_make_message(text, chat_id), uuid4())
        responses.append(r)
        return r

    # Turn 1 — greeting
    r1 = await send(
        "Привет, хочу записаться на танцы",
        _llm_resp("Привет! В каком филиале удобнее заниматься?",
                  slot_updates={}),
    )
    assert any(kw in r1.lower() for kw in ["филиал", "семёновск", "гоголя", "привет"]), \
        f"Expected branch question, got: {r1!r}"

    # Turn 2 — branch (use "Тест" so confirm_booking succeeds when booking_branch is set)
    r2 = await send(
        "В филиал Тест",
        _llm_resp("Отлично! Какое направление тебя интересует?",
                  slot_updates={"branch": "Тест"}),
    )
    assert any(kw in r2.lower() for kw in ["направлен", "стиль", "high heels", "отлично"]), \
        f"Expected direction question, got: {r2!r}"

    # Turn 3 — direction + experience → LLM calls get_filtered_schedule, then second LLM call.
    # generate_schedule_response is patched to return a plain string (no real CRM needed).
    # Second LLM response must NOT mention specific times to avoid G12 violation.
    first_resp = _llm_resp(
        "Смотрю расписание для тебя!",
        slot_updates={"group": "High Heels", "experience": "новичок"},
        tool_calls=[{"name": "get_filtered_schedule", "parameters": {"style": "High Heels"}}],
    )
    second_resp = _llm_resp(
        "Нашла занятия в ближайшие дни. Когда тебе удобнее?",
        slot_updates={"schedule_shown": True},
    )
    mock_llm.call = AsyncMock(side_effect=[first_resp, second_resp])

    with patch("app.core.engine.generate_schedule_response",
               new=AsyncMock(return_value="Расписание High Heels: есть места на этой неделе.")):
        r3 = await engine.handle_message(_make_message("High Heels, я новичок", chat_id), uuid4())
    responses.append(r3)

    # LLM must have been called twice: once for initial response, once after tool result injected
    assert mock_llm.call.call_count == 2, \
        f"Expected 2 LLM calls (tool path), got {mock_llm.call.call_count}"
    assert r3 is not None

    # Turn 4 — pick date
    r4 = await send(
        "Давайте в пятницу",
        _llm_resp("Как тебя зовут и номер телефона?",
                  slot_updates={"datetime_raw": "пятница 19:00"}),
    )
    assert any(kw in r4.lower() for kw in ["имя", "зовут", "телефон", "номер"]), \
        f"Expected contact question, got: {r4!r}"

    # Turn 5 — contact info
    # Pass datetime_resolved as a datetime object (not ISO string) so SlotValues stores it correctly
    future_dt = (datetime.now(timezone.utc) + timedelta(days=7)).replace(
        hour=19, minute=0, second=0, microsecond=0
    )
    r5 = await send(
        "Маша Иванова, 89241234567",
        _llm_resp(
            "Подтверди запись:\nHigh Heels\nМаша Иванова, 89241234567",
            intent="continue",
            slot_updates={
                "client_name": "Маша Иванова",
                "client_phone": "89241234567",
                "datetime_resolved": future_dt,
                "summary_shown": True,
            },
        ),
    )
    assert any(kw in r5.lower() for kw in ["подтверди", "маша", "high heels", "89241234567"]), \
        f"Expected confirmation summary, got: {r5!r}"

    # Turn 6 — "да" hits _CONFIRM_YES fast path → confirm_booking() called directly, no LLM
    mock_llm.call = AsyncMock()  # fresh mock with no return value configured
    r6 = await engine.handle_message(_make_message("Да", chat_id), uuid4())
    responses.append(r6)
    # LLM must NOT have been called — fast path bypasses it entirely
    mock_llm.call.assert_not_called()

    # Receipt must contain branch (Тест) and dress code
    assert "Тест" in r6, f"Receipt missing branch: {r6!r}"
    assert any(kw in r6.lower() for kw in ["носочки", "каблуки", "подошв"]), \
        f"Receipt missing dress code: {r6!r}"

    # Total turns ≤ 8
    assert len(responses) <= 8, f"Too many turns: {len(responses)}"


# ---------------------------------------------------------------------------
# Test 2 — Direction help (heels vs sneakers)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_direction_help(kb, mock_impulse, mock_llm):
    """Engine helps undecided user pick a direction without dumping the full list."""
    chat_id = "smoke_direction"

    engine = ConversationEngine(
        llm_router=mock_llm,
        prompt_builder=PromptBuilder(kb),
        guardrails=GuardrailRunner(kb),
        impulse_adapter=mock_impulse,
        kb=kb,
    )

    # Turn 1 — undecided user
    mock_llm.call = AsyncMock(return_value=_llm_resp(
        "Ты предпочитаешь каблуки или кроссовки?",
        slot_updates={},
    ))
    r1 = await engine.handle_message(
        _make_message("Хочу на танцы, не знаю что выбрать", chat_id), uuid4()
    )
    # Should ask a clarifying question, not dump the full list
    assert any(kw in r1.lower() for kw in ["каблук", "кроссовк", "женственн", "энергичн"]), \
        f"Expected style question, got: {r1!r}"
    full_list = "High Heels, Girly Hip-Hop, Frame Up Strip, Dancehall"
    assert full_list not in r1, f"Should not dump full direction list: {r1!r}"

    # Turn 2 — user picks heels
    mock_llm.call = AsyncMock(return_value=_llm_resp(
        "Тогда тебе подойдёт High Heels или Frame Up Strip — оба на каблуках.",
        slot_updates={},
    ))
    r2 = await engine.handle_message(
        _make_message("На каблуках", chat_id), uuid4()
    )
    assert any(kw in r2.lower() for kw in ["high heels", "frame up", "каблук"]), \
        f"Expected heels recommendation, got: {r2!r}"


# ---------------------------------------------------------------------------
# Test 3 — Cancel flow delegation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_flow_delegation(kb, mock_impulse, mock_llm):
    """Cancel intent is delegated to cancel_flow.py, not handled by LLM."""
    from app.core.conversation import get_or_create_session, update_slots
    from app.models import ConversationState
    chat_id = "smoke_cancel"

    engine = ConversationEngine(
        llm_router=mock_llm,
        prompt_builder=PromptBuilder(kb),
        guardrails=GuardrailRunner(kb),
        impulse_adapter=mock_impulse,
        kb=kb,
    )

    # Pre-fill session: phone known, CANCEL_FLOW phase
    session = await get_or_create_session(str(uuid4()), "telegram", chat_id)
    await update_slots(session,
                       client_phone="89241234567",
                       cancel_bookings=[])

    # cancel_flow.start() will be called because compute_phase returns CANCEL_FLOW
    # when is_cancel=True. We simulate this by patching compute_phase.
    # Give the engine a fresh LLM mock so call_count starts at 0
    fresh_llm_call = AsyncMock()
    engine._llm.call = fresh_llm_call  # engine._llm IS mock_llm (same object from constructor)

    with patch("app.core.engine.compute_phase") as mock_phase:
        from app.core.slot_tracker import ConversationPhase
        mock_phase.return_value = ConversationPhase.CANCEL_FLOW

        # cancel_flow.start() returns "нет записей" for empty list
        r = await engine.handle_message(
            _make_message("Хочу отменить запись", chat_id), uuid4()
        )

    # LLM must NOT have been called — cancel_flow handled it entirely
    # fresh_llm_call is the same attribute as engine._llm.call, so this is unambiguous
    fresh_llm_call.assert_not_called()
    # Response should come from cancel_flow (no bookings found)
    assert any(kw in r.lower() for kw in ["записей", "отмен", "телефон", "администратор"]), \
        f"Expected cancel_flow response, got: {r!r}"
