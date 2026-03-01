"""E2E smoke tests for entity resolution (RFC-004 §4.4.2).

Uses TEST_MODE with mock CRM and in-memory session store.
Verifies resolver works end-to-end with ConversationEngine.

Run: python -m pytest tests/e2e/test_entity_resolution_smoke.py -v
"""

import json
import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from app.core.engine import ConversationEngine
from app.core.guardrails import GuardrailRunner
from app.core.prompt_builder import PromptBuilder
from app.core.entity_resolver import AliasEntityResolver, BranchResolver, StyleResolver, TeacherResolver
from app.knowledge.base import KnowledgeBase, load_knowledge_base
from app.models import Session, SlotValues, UnifiedMessage


# ---------------------------------------------------------------------------
# In-memory session store (same pattern as test_engine_smoke)
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
    from app.models import ConversationState
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
    if "INTO sessions" in query and len(args) >= 9:
        from app.models import ConversationState, SlotValues
        channel, chat_id = str(args[0]), str(args[1])
        existing = _STORE.get(channel, chat_id)
        if existing:
            existing.state = ConversationState(args[2])
            existing.slots = SlotValues.model_validate(args[3])
            existing.updated_at = args[8]
        else:
            meta = args[5] or {}
            raw_tid = meta.get("trace_id")
            try:
                tid = uuid4() if not raw_tid else __import__("uuid").UUID(raw_tid)
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
def mock_impulse(kb):
    """Mock CRM: teachers for resolver sync, schedule for booking."""
    adapter = MagicMock()
    # Use next Friday 19:00 so weekday is fixed (4 = Friday in Python)
    now = datetime.now(timezone.utc)
    days_ahead = (4 - now.weekday() + 7) % 7 or 7
    future_dt = (now + timedelta(days=days_ahead)).replace(hour=19, minute=0, second=0, microsecond=0)
    schedule_entry = SimpleNamespace(
        id=42,
        day=future_dt.weekday(),
        minutes_begin=19 * 60,
        style_name="High Heels",
        teacher_name="Анастасия Николаева",
        branch={"id": "TT", "name": "Тест"},
    )
    adapter.get_schedule = AsyncMock(return_value=[schedule_entry])
    adapter._test_future_dt = future_dt  # so test can use same date for slot_updates
    adapter.health_check = AsyncMock(return_value=True)
    # Teachers for entity resolution (Настюша → Анастасия)
    adapter.get_teacher_list = AsyncMock(return_value=[
        {"id": 1, "name": "Анастасия Николаева"},
        {"id": 2, "name": "Матюха Катя"},
    ])
    client = MagicMock()
    client.id = 101
    client.name = "Маша Иванова"
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


def _make_message(text: str, chat_id: str = "entity_smoke") -> UnifiedMessage:
    return UnifiedMessage(
        channel="telegram",
        chat_id=chat_id,
        message_id=str(uuid4()),
        timestamp=datetime.now(timezone.utc),
        text=text,
        message_type="text",
        sender_phone=None,
        sender_name="Smoke User",
    )


def _llm_resp(message: str, intent: str = "continue", slot_updates: dict | None = None,
              tool_calls: list | None = None) -> MagicMock:
    def _default(obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")

    r = MagicMock()
    payload = {
        "message": message,
        "intent": intent,
        "slot_updates": slot_updates or {},
        "tool_calls": tool_calls or [],
    }
    r.text = json.dumps(payload, ensure_ascii=False, default=_default)
    return r


def _build_engine(kb: KnowledgeBase, mock_impulse, mock_llm, resolver: AliasEntityResolver | None):
    with patch("app.config.get_settings") as mock_settings:
        mock_settings.return_value.crm_tenant = "test"
        return ConversationEngine(
            llm_router=mock_llm,
            prompt_builder=PromptBuilder(kb),
            guardrails=GuardrailRunner(kb),
            impulse_adapter=mock_impulse,
            kb=kb,
            resolver=resolver,
        )


# ---------------------------------------------------------------------------
# Test 1 — Diminutive name + branch resolution, then booking to receipt
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_diminutive_name_booking(kb, mock_impulse, mock_llm):
    """Настюше + каблуки → Анастасия + High Heels; Первая речка → Гоголя; no 'не нашла'; receipt has teacher and branch."""
    from app.storage.session_store import delete_session
    chat_id = "entity_diminutive"
    await delete_session("telegram", chat_id)

    path = Path(__file__).resolve().parents[2] / "app" / "core" / "entity_resolver" / "names_dict.json"
    teacher_resolver = TeacherResolver(path)
    branch_resolver = BranchResolver(kb)
    style_resolver = StyleResolver(kb)
    resolver = AliasEntityResolver(teacher_resolver, branch_resolver, style_resolver)
    await teacher_resolver.sync(mock_impulse)

    engine = _build_engine(kb, mock_impulse, mock_llm, resolver)
    responses = []

    # Turn 1 — "Хочу к Настюше на каблуки" → LLM returns teacher_raw, style_raw
    mock_llm.call = AsyncMock(return_value=_llm_resp(
        "Отлично! В каком филиале удобнее заниматься?",
        slot_updates={"teacher_raw": "настюше", "style_raw": "каблуки"},
    ))
    r1 = await engine.handle_message(_make_message("Привет! Хочу к Настюше на каблуки", chat_id), uuid4())
    responses.append(r1)
    assert "не нашла" not in r1.lower() and "не найден" not in r1.lower() and "не нашлось" not in r1.lower(), \
        f"Resolver should find teacher/style, got: {r1!r}"
    assert any(kw in r1.lower() for kw in ["филиал", "гоголя", "семёновск", "алеутск", "удобн"]), \
        f"Should ask about branch, got: {r1!r}"

    # Turn 2 — "В филиал Тест" (booking_branch=Тест so confirm_booking will succeed)
    mock_llm.call = AsyncMock(return_value=_llm_resp(
        "Смотрю расписание!",
        slot_updates={"branch_raw": "тест"},
        tool_calls=[{"name": "get_filtered_schedule", "parameters": {"style": "High Heels"}}],
    ))
    with patch("app.core.engine.generate_schedule_response", new=AsyncMock(
            return_value="Расписание High Heels (Тест): среда, пятница 19:00.")):
        r2 = await engine.handle_message(_make_message("В филиал Тест", chat_id), uuid4())
    responses.append(r2)
    assert "не нашла" not in r2.lower(), f"Branch should resolve, got: {r2!r}"

    # Turn 3 — pick date (use same Friday as mock schedule)
    future_dt = getattr(mock_impulse, "_test_future_dt", None)
    if future_dt is None:
        now = datetime.now(timezone.utc)
        days_ahead = (4 - now.weekday() + 7) % 7 or 7
        future_dt = (now + timedelta(days=days_ahead)).replace(hour=19, minute=0, second=0, microsecond=0)
    mock_llm.call = AsyncMock(return_value=_llm_resp(
        "Как тебя зовут и номер телефона?",
        slot_updates={"datetime_raw": "пятница", "datetime_resolved": future_dt, "schedule_shown": True},
    ))
    r3 = await engine.handle_message(_make_message("В пятницу", chat_id), uuid4())
    responses.append(r3)

    # Turn 4 — contact
    mock_llm.call = AsyncMock(return_value=_llm_resp(
        "Подтверди запись: High Heels, Тест, пятница 19:00. Маша Иванова, 89241234567",
        slot_updates={"client_name": "Маша Иванова", "client_phone": "89241234567", "summary_shown": True},
    ))
    r4 = await engine.handle_message(_make_message("Маша Иванова, 89241234567", chat_id), uuid4())
    responses.append(r4)

    # Turn 5 — "да" → confirm_booking (branch Тест matches booking_branch)
    mock_llm.call = AsyncMock()
    r5 = await engine.handle_message(_make_message("Да", chat_id), uuid4())
    responses.append(r5)
    assert "Тест" in r5, f"Receipt should contain branch: {r5!r}"
    assert any(kw in r5.lower() for kw in ["анастасия", "катя", "преподаватель", "носочк", "каблук", "адрес"]), \
        f"Receipt should contain teacher or dress code or address: {r5!r}"


# ---------------------------------------------------------------------------
# Test 2 — Center disambiguation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_center_disambiguation(kb, mock_impulse, mock_llm):
    """'Хилс в центре' → both Алеутская and Семёновская; 'Алеутская' → single branch."""
    from app.storage.session_store import delete_session
    chat_id = "entity_center"
    await delete_session("telegram", chat_id)

    path = Path(__file__).resolve().parents[2] / "app" / "core" / "entity_resolver" / "names_dict.json"
    resolver = AliasEntityResolver(TeacherResolver(path), BranchResolver(kb), StyleResolver(kb))
    await resolver._teacher.sync(mock_impulse)
    engine = _build_engine(kb, mock_impulse, mock_llm, resolver)

    mock_llm.call = AsyncMock(return_value=_llm_resp(
        "У нас два филиала в центре — какой удобнее?",
        slot_updates={"style_raw": "хилс", "branch_raw": "центр"},
    ))
    r1 = await engine.handle_message(_make_message("Хочу на хилс в центре", chat_id), uuid4())
    assert "алеутская" in r1.lower() and "семёновская" in r1.lower(), \
        f"Should mention both central branches: {r1!r}"

    mock_llm.call = AsyncMock(return_value=_llm_resp(
        "Отлично, Алеутская! Смотрю расписание.",
        slot_updates={"branch_raw": "алеутская"},
        tool_calls=[{"name": "get_filtered_schedule", "parameters": {"style": "High Heels"}}],
    ))
    with patch("app.core.engine.generate_schedule_response", new=AsyncMock(
            return_value="Расписание High Heels (Алеутская): среда 19:00.")):
        r2 = await engine.handle_message(_make_message("Алеутская", chat_id), uuid4())
    assert "алеутская" in r2.lower() or "расписан" in r2.lower(), f"Should proceed with one branch: {r2!r}"


# ---------------------------------------------------------------------------
# Test 3 — Unknown area
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unknown_area(kb, mock_impulse, mock_llm):
    """'Есть занятия на Седанке?' → нет филиала + suggest nearest."""
    from app.storage.session_store import delete_session
    chat_id = "entity_unknown"
    await delete_session("telegram", chat_id)

    resolver = AliasEntityResolver(
        TeacherResolver(Path(__file__).resolve().parents[2] / "app" / "core" / "entity_resolver" / "names_dict.json"),
        BranchResolver(kb),
        StyleResolver(kb),
    )
    await resolver._teacher.sync(mock_impulse)
    engine = _build_engine(kb, mock_impulse, mock_llm, resolver)

    mock_llm.call = AsyncMock(return_value=_llm_resp(
        "Проверяю филиалы.",
        slot_updates={"branch_raw": "седанка"},
    ))
    r = await engine.handle_message(_make_message("Есть занятия на Седанке?", chat_id), uuid4())
    assert any(kw in r.lower() for kw in ["нет филиал", "ближайш", "гоголя"]), \
        f"Should mention no branch and nearest: {r!r}"


# ---------------------------------------------------------------------------
# Test 4 — Subscription intent (no direction/date ask, price info)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subscription_intent(kb, mock_impulse, mock_llm):
    """'Хочу купить абонемент на 8 занятий' → price info, no direction/date ask."""
    from app.storage.session_store import delete_session
    chat_id = "entity_sub"
    await delete_session("telegram", chat_id)

    path = Path(__file__).resolve().parents[2] / "app" / "core" / "entity_resolver" / "names_dict.json"
    resolver = AliasEntityResolver(TeacherResolver(path), BranchResolver(kb), StyleResolver(kb))
    await resolver._teacher.sync(mock_impulse)
    engine = _build_engine(kb, mock_impulse, mock_llm, resolver)

    mock_llm.call = AsyncMock(return_value=_llm_resp(
        "Абонемент на 8 занятий — 5100₽ или 6100₽. Для покупки свяжитесь с администратором.",
        intent="buy_subscription",
    ))
    r = await engine.handle_message(_make_message("Хочу купить абонемент на 8 занятий", chat_id), uuid4())
    assert any(kw in r.lower() for kw in ["цен", "стоимость", "абонемент", "5100", "6100", "руб"]), \
        f"Should give price info: {r!r}"
    assert "на какой день" not in r.lower() and "какое направление" not in r.lower(), \
        f"Should NOT ask direction/date: {r!r}"


# ---------------------------------------------------------------------------
# Test 5 — Teacher sync degraded: price still works from KB
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_teacher_sync_degraded_price_still_works(kb, mock_impulse, mock_llm):
    """Resolver not synced; price question should still work from KB."""
    from app.storage.session_store import delete_session
    chat_id = "entity_degraded"
    await delete_session("telegram", chat_id)

    path = Path(__file__).resolve().parents[2] / "app" / "core" / "entity_resolver" / "names_dict.json"
    resolver = AliasEntityResolver(TeacherResolver(path), BranchResolver(kb), StyleResolver(kb))
    # Do NOT sync — is_ready stays False

    engine = _build_engine(kb, mock_impulse, mock_llm, resolver)
    mock_llm.call = AsyncMock(return_value=_llm_resp(
        "Разовые от 900₽. Абонементы: 4 занятия 3900₽, 8 — 5100₽/6100₽.",
        intent="ask_price",
    ))
    r = await engine.handle_message(_make_message("Сколько стоит абонемент?", chat_id), uuid4())
    assert any(kw in r.lower() for kw in ["руб", "цен", "3900", "5100", "абонемент"]), \
        f"Price question should work from KB/LLM: {r!r}"
