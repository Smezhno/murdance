"""Unit tests for GuardrailRunner (RFC-003 §6.1).

Each guardrail is tested individually with crafted LLMResponse objects.
"""

import pytest
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from app.core.guardrails import GuardrailRunner, GuardrailResult
from app.core.prompt_builder import LLMResponse, ToolCall
from app.core.slot_tracker import ConversationPhase
from app.knowledge.base import KnowledgeBase
from app.models import SlotValues

_STUDIO_TZ = ZoneInfo("Asia/Vladivostok")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def kb() -> KnowledgeBase:
    return KnowledgeBase(
        schema_version="1.0",
        studio={
            "name": "She Dance",
            "schedule": "CRM",
            "timezone": "Asia/Vladivostok",
            "branches": [{"name": "Тест", "address": "Тест, 1", "phone": "+7 000", "halls": []}],
        },
        tone={"style": "friendly", "pronouns": "ты"},
        services=[
            {"id": "high-heels", "name": "High Heels", "description": "Каблуки",
             "price_single": 900, "aliases": ["хай хилс", "high heels"]},
            {"id": "girly-hiphop", "name": "Girly Hip-Hop", "description": "Хип-хоп",
             "price_single": 900, "aliases": ["герли"]},
        ],
        teachers=[
            {"id": "katya", "name": "Катя Смирнова", "styles": ["High Heels"],
             "specialization": "Преп.", "aliases": ["катя"]},
            {"id": "anna", "name": "Анна Ким", "styles": ["Girly Hip-Hop"],
             "specialization": "Преп.", "aliases": ["анна"]},
        ],
        escalation={"triggers": ["жалоба"]},
        branches=[
            {
                "id": "semenovskaya",
                "name": "Семёновская",
                "crm_branch_id": "ZZ",
                "address": "Семёновская 30а (стеклянное здание, крайняя дверь справа)",
                "styles": ["high-heels"],
                "aliases": ["семёновская", "семеновская"],
            },
        ],
        dress_code={
            "high-heels": "носочки либо каблуки со светлой подошвой",
        },
        subscriptions=[
            {"name": "Разовое", "price": 900, "classes": 1, "validity_days": 1, "category": "разовые"},
            {"name": "4 занятия", "price": 3900, "classes": 4, "validity_days": 30, "category": "групповые"},
        ],
    )


@pytest.fixture
def runner(kb: KnowledgeBase) -> GuardrailRunner:
    return GuardrailRunner(kb)


def _resp(message: str = "Привет!", intent: str = "continue",
          slot_updates: dict | None = None, tool_calls: list[ToolCall] | None = None) -> LLMResponse:
    return LLMResponse(
        message=message,
        intent=intent,
        slot_updates=slot_updates or {},
        tool_calls=tool_calls or [],
    )


def _future_dt() -> datetime:
    return datetime.now(_STUDIO_TZ) + timedelta(days=7)


def _past_dt() -> datetime:
    return datetime.now(_STUDIO_TZ) - timedelta(days=1)


_CRM_SCHEDULE = [
    {"id": "42", "date": "2026-03-07", "time": "19:00",
     "group": "High Heels", "teacher": "Катя", "available_spots": 3},
    {"id": "43", "date": "2026-03-09", "time": "11:00",
     "group": "High Heels", "teacher": "Катя", "available_spots": 5},
]


# ---------------------------------------------------------------------------
# G1 — Schedule times must exist in CRM
# ---------------------------------------------------------------------------

def _has(violations: list[str], code: str) -> bool:
    """Check that a violation with exactly this code prefix exists (avoids G1/G12 collision)."""
    return any(v.startswith(f"{code}:") for v in violations)


class TestG1:
    @pytest.mark.asyncio
    async def test_known_time_passes(self, runner):
        tc = ToolCall(name="get_filtered_schedule", parameters={"style": "High Heels"})
        r = await runner.check(
            _resp("Ближайшее занятие в 19:00 в пятницу", tool_calls=[tc]),
            SlotValues(), ConversationPhase.SCHEDULE, crm_schedule=_CRM_SCHEDULE,
        )
        assert not _has(r.violations, "G1")

    @pytest.mark.asyncio
    async def test_unknown_time_blocks(self, runner):
        tc = ToolCall(name="get_filtered_schedule")
        r = await runner.check(
            _resp("Занятие в 15:00 в среду", tool_calls=[tc]),
            SlotValues(), ConversationPhase.SCHEDULE, crm_schedule=_CRM_SCHEDULE,
        )
        assert _has(r.violations, "G1")
        assert not r.passed

    @pytest.mark.asyncio
    async def test_no_crm_schedule_skips_g1(self, runner):
        tc = ToolCall(name="get_filtered_schedule")
        r = await runner.check(
            _resp("Занятие в 15:00", tool_calls=[tc]),
            SlotValues(), ConversationPhase.SCHEDULE,
        )
        assert not _has(r.violations, "G1")

    @pytest.mark.asyncio
    async def test_g1_silent_when_g12_fires(self, runner):
        r = await runner.check(
            _resp("Занятие в 15:00"),  # no tool_call
            SlotValues(), ConversationPhase.SCHEDULE, crm_schedule=_CRM_SCHEDULE,
        )
        assert _has(r.violations, "G12")
        assert not _has(r.violations, "G1")

    @pytest.mark.asyncio
    async def test_g1_passes_when_tool_called_rfc006(self, runner):
        """G1 should pass when get_filtered_schedule was executed (RFC-006: crm_schedule=None)."""
        tc = ToolCall(name="get_filtered_schedule", parameters={"style": "High Heels"})
        r = await runner.check(
            _resp("Ближайшее занятие в 19:00 в пятницу", tool_calls=[tc]),
            SlotValues(), ConversationPhase.SCHEDULE,
            crm_schedule=None,
            executed_tools={"get_filtered_schedule"},
        )
        assert not _has(r.violations, "G1")


# ---------------------------------------------------------------------------
# G2 — Prices must match KB (±50₽)
# ---------------------------------------------------------------------------

class TestG2:
    @pytest.mark.asyncio
    async def test_known_price_passes(self, runner):
        r = await runner.check(_resp("Разовое занятие стоит 900₽"), SlotValues(), ConversationPhase.GREETING)
        assert not any("G2" in v for v in r.violations)

    @pytest.mark.asyncio
    async def test_price_within_tolerance_passes(self, runner):
        r = await runner.check(_resp("Занятие стоит 920₽"), SlotValues(), ConversationPhase.GREETING)
        assert not any("G2" in v for v in r.violations)

    @pytest.mark.asyncio
    async def test_invented_price_blocks(self, runner):
        r = await runner.check(_resp("Занятие стоит 500₽"), SlotValues(), ConversationPhase.GREETING)
        assert any("G2" in v for v in r.violations)
        assert not r.passed

    @pytest.mark.asyncio
    async def test_short_number_not_caught(self, runner):
        """Two-digit numbers must not trigger G2 (regex requires 3+ digits)."""
        r = await runner.check(_resp("Занятие в 8₽"), SlotValues(), ConversationPhase.GREETING)
        assert not any("G2" in v for v in r.violations)

    @pytest.mark.asyncio
    async def test_no_price_skips_g2(self, runner):
        r = await runner.check(_resp("Привет!"), SlotValues(), ConversationPhase.GREETING)
        assert not any("G2" in v for v in r.violations)


# ---------------------------------------------------------------------------
# G3 — Required slots before booking
# ---------------------------------------------------------------------------

class TestG3:
    @pytest.mark.asyncio
    async def test_missing_phone_blocks(self, runner):
        slots = SlotValues(group="High Heels", client_name="Маша",
                           datetime_resolved=_future_dt())
        tc = ToolCall(name="create_booking", parameters={"schedule_id": "42"})
        r = await runner.check(_resp(intent="booking", tool_calls=[tc]), slots, ConversationPhase.CONFIRMATION)
        assert any("G3" in v for v in r.violations)

    @pytest.mark.asyncio
    async def test_all_slots_filled_passes(self, runner):
        slots = SlotValues(group="High Heels", client_name="Маша",
                           client_phone="89241234567", datetime_resolved=_future_dt())
        r = await runner.check(_resp(intent="booking"), slots, ConversationPhase.CONFIRMATION)
        assert not any("G3" in v for v in r.violations)

    @pytest.mark.asyncio
    async def test_create_booking_tool_triggers_g3(self, runner):
        slots = SlotValues(group="High Heels", datetime_resolved=_future_dt())
        tc = ToolCall(name="create_booking", parameters={"schedule_id": "42"})
        r = await runner.check(_resp(tool_calls=[tc]), slots, ConversationPhase.CONFIRMATION)
        assert any("G3" in v for v in r.violations)

    @pytest.mark.asyncio
    async def test_non_booking_intent_skips_g3(self, runner):
        r = await runner.check(_resp(intent="continue"), SlotValues(), ConversationPhase.DISCOVERY)
        assert not any("G3" in v for v in r.violations)


# ---------------------------------------------------------------------------
# G3b — No receipt/address/dress code before name and phone
# ---------------------------------------------------------------------------

class TestG3b:
    @pytest.mark.asyncio
    async def test_receipt_phrase_without_contact_blocks(self, runner):
        slots = SlotValues(group="High Heels", datetime_resolved=_future_dt(), branch="Тест")
        r = await runner.check(
            _resp(message="Запись подтверждена! Адрес: Тест, 1. С собой: носочки."),
            slots, ConversationPhase.CONFIRMATION,
        )
        assert any("G3b" in v for v in r.violations)

    @pytest.mark.asyncio
    async def test_dress_code_without_contact_blocks(self, runner):
        slots = SlotValues(group="High Heels", datetime_resolved=_future_dt())
        r = await runner.check(
            _resp(message="Отлично! Владивосток, филиал Тест. Что взять с собой: носочки."),
            slots, ConversationPhase.CONFIRMATION,
        )
        assert any("G3b" in v for v in r.violations)

    @pytest.mark.asyncio
    async def test_ask_name_phone_passes(self, runner):
        slots = SlotValues(group="High Heels", datetime_resolved=_future_dt())
        r = await runner.check(
            _resp(message="Как вас зовут и какой номер телефона для связи?"),
            slots, ConversationPhase.CONFIRMATION,
        )
        assert not any("G3b" in v for v in r.violations)

    @pytest.mark.asyncio
    async def test_receipt_with_contact_passes(self, runner):
        slots = SlotValues(group="High Heels", client_name="Маша",
                           client_phone="89241234567", datetime_resolved=_future_dt())
        r = await runner.check(
            _resp(message="✅ Запись подтверждена! С собой: носочки."),
            slots, ConversationPhase.BOOKING,
        )
        assert not any("G3b" in v for v in r.violations)

    @pytest.mark.asyncio
    async def test_g3b_skips_discovery(self, runner):
        slots = SlotValues()
        r = await runner.check(
            _resp(message="Что взять с собой: удобная форма."),
            slots, ConversationPhase.DISCOVERY,
        )
        assert not any("G3b" in v for v in r.violations)


# ---------------------------------------------------------------------------
# G4 — create_booking only after confirmed=True
# ---------------------------------------------------------------------------

class TestG4:
    @pytest.mark.asyncio
    async def test_confirmed_false_blocks(self, runner):
        slots = SlotValues(confirmed=False)
        tc = ToolCall(name="create_booking")
        r = await runner.check(_resp(tool_calls=[tc]), slots, ConversationPhase.BOOKING)
        assert any("G4" in v for v in r.violations)

    @pytest.mark.asyncio
    async def test_confirmed_true_passes(self, runner):
        slots = SlotValues(group="High Heels", client_name="Маша",
                           client_phone="89241234567", datetime_resolved=_future_dt(),
                           confirmed=True)
        tc = ToolCall(name="create_booking", parameters={"schedule_id": "42"})
        r = await runner.check(
            _resp(tool_calls=[tc]), slots, ConversationPhase.BOOKING,
            crm_schedule=_CRM_SCHEDULE,
        )
        assert not any("G4" in v for v in r.violations)

    @pytest.mark.asyncio
    async def test_no_create_booking_skips_g4(self, runner):
        r = await runner.check(_resp(), SlotValues(), ConversationPhase.DISCOVERY)
        assert not any("G4" in v for v in r.violations)


# ---------------------------------------------------------------------------
# G5 — No teacher comparisons
# ---------------------------------------------------------------------------

class TestG5:
    @pytest.mark.asyncio
    async def test_two_teachers_with_comparison_blocks(self, runner):
        r = await runner.check(
            _resp("Катя лучше чем Анна"), SlotValues(), ConversationPhase.DISCOVERY,
        )
        assert any("G5" in v for v in r.violations)

    @pytest.mark.asyncio
    async def test_single_teacher_no_comparison_passes(self, runner):
        r = await runner.check(
            _resp("Катя ведёт занятия по High Heels"), SlotValues(), ConversationPhase.DISCOVERY,
        )
        assert not any("G5" in v for v in r.violations)

    @pytest.mark.asyncio
    async def test_comparison_word_without_teachers_passes(self, runner):
        r = await runner.check(
            _resp("Это лучше всего подходит для новичков"), SlotValues(), ConversationPhase.DISCOVERY,
        )
        assert not any("G5" in v for v in r.violations)

    @pytest.mark.asyncio
    async def test_attribute_adjective_with_two_teachers_passes(self, runner):
        r = await runner.check(
            _resp("Катя ведёт лучшие группы, Анна тоже хороший педагог"),
            SlotValues(), ConversationPhase.DISCOVERY,
        )
        assert not any("G5" in v for v in r.violations)


# ---------------------------------------------------------------------------
# G7 — Auto-fix: truncate > 300 chars at sentence boundary
# ---------------------------------------------------------------------------

class TestG7:
    @pytest.mark.asyncio
    async def test_short_message_unchanged(self, runner):
        msg = "Привет! Как дела?"
        r = await runner.check(_resp(msg), SlotValues(), ConversationPhase.GREETING)
        assert r.corrected_message is None

    @pytest.mark.asyncio
    async def test_long_message_truncated(self, runner):
        msg = ("А" * 280) + ". " + ("Б" * 50) + "."
        r = await runner.check(_resp(msg), SlotValues(), ConversationPhase.GREETING)
        assert r.corrected_message is not None
        assert len(r.corrected_message) <= 300

    @pytest.mark.asyncio
    async def test_truncation_at_sentence_boundary(self, runner):
        msg = "Первое предложение. " + "Б" * 290 + "."
        r = await runner.check(_resp(msg), SlotValues(), ConversationPhase.GREETING)
        assert r.corrected_message is not None
        assert r.corrected_message.endswith(".")
        assert "Первое предложение." in r.corrected_message

    @pytest.mark.asyncio
    async def test_no_boundary_hard_cuts_at_300(self, runner):
        msg = "А" * 400
        r = await runner.check(_resp(msg), SlotValues(), ConversationPhase.GREETING)
        assert r.corrected_message is not None
        assert len(r.corrected_message) == 300

    @pytest.mark.asyncio
    async def test_g7_does_not_affect_violations(self, runner):
        """G7 is auto-fix only — must not add violations."""
        msg = "А" * 400
        r = await runner.check(_resp(msg), SlotValues(), ConversationPhase.GREETING)
        assert not any("G7" in v for v in r.violations)


# ---------------------------------------------------------------------------
# G8 — Auto-fix: strip emoji beyond 2
# ---------------------------------------------------------------------------

class TestG8:
    @pytest.mark.asyncio
    async def test_two_emoji_unchanged(self, runner):
        r = await runner.check(_resp("Привет! 😊🎉"), SlotValues(), ConversationPhase.GREETING)
        assert r.corrected_message is None

    @pytest.mark.asyncio
    async def test_three_emoji_stripped_to_two(self, runner):
        r = await runner.check(_resp("Привет! 😊🎉🌟"), SlotValues(), ConversationPhase.GREETING)
        assert r.corrected_message is not None
        emoji_count = sum(1 for ch in r.corrected_message if _is_emoji_char(ch))
        assert emoji_count == 2

    @pytest.mark.asyncio
    async def test_no_emoji_unchanged(self, runner):
        r = await runner.check(_resp("Привет!"), SlotValues(), ConversationPhase.GREETING)
        assert r.corrected_message is None

    @pytest.mark.asyncio
    async def test_g8_does_not_affect_violations(self, runner):
        r = await runner.check(_resp("😊🎉🌟💃🕺"), SlotValues(), ConversationPhase.GREETING)
        assert not any("G8" in v for v in r.violations)


# ---------------------------------------------------------------------------
# G9 — POST_BOOKING receipt must contain address + dress code
# ---------------------------------------------------------------------------

class TestG9:
    @pytest.mark.asyncio
    async def test_missing_address_blocks(self, runner):
        slots = SlotValues(branch="Семёновская", group="High Heels", booking_created=True)
        msg = "Запись подтверждена! Возьми носочки либо каблуки со светлой подошвой."
        r = await runner.check(_resp(msg), slots, ConversationPhase.POST_BOOKING)
        assert any("G9" in v for v in r.violations)

    @pytest.mark.asyncio
    async def test_missing_dresscode_blocks(self, runner):
        slots = SlotValues(branch="Семёновская", group="High Heels", booking_created=True)
        msg = "Запись подтверждена! Адрес: Семёновская 30а (стеклянное здание, крайняя дверь справа)."
        r = await runner.check(_resp(msg), slots, ConversationPhase.POST_BOOKING)
        assert any("G9" in v for v in r.violations)

    @pytest.mark.asyncio
    async def test_full_receipt_passes(self, runner):
        slots = SlotValues(branch="Семёновская", group="High Heels", booking_created=True)
        msg = ("Запись подтверждена! "
               "Адрес: Семёновская 30а (стеклянное здание, крайняя дверь справа). "
               "С собой: носочки либо каблуки со светлой подошвой.")
        r = await runner.check(_resp(msg), slots, ConversationPhase.POST_BOOKING)
        assert not any("G9" in v for v in r.violations)

    @pytest.mark.asyncio
    async def test_non_post_booking_phase_skips_g9(self, runner):
        slots = SlotValues(branch="Семёновская", group="High Heels")
        r = await runner.check(_resp("Привет!"), slots, ConversationPhase.DISCOVERY)
        assert not any("G9" in v for v in r.violations)

    @pytest.mark.asyncio
    async def test_non_receipt_post_booking_skips_g9(self, runner):
        """Regular POST_BOOKING answer (no receipt marker) must not trigger G9."""
        slots = SlotValues(branch="Семёновская", group="High Heels", booking_created=True)
        msg = "Адрес студии: Семёновская 30а. Возьми носочки или каблуки."
        r = await runner.check(_resp(msg), slots, ConversationPhase.POST_BOOKING)
        assert not any("G9" in v for v in r.violations)

    @pytest.mark.asyncio
    async def test_partial_address_passes_g9(self, runner):
        slots = SlotValues(branch="Семёновская", group="High Heels", booking_created=True)
        msg = "✅ Запись подтверждена! Адрес: Семёновская 30а. С собой: носочки либо каблуки."
        r = await runner.check(_resp(msg), slots, ConversationPhase.POST_BOOKING)
        assert not any("G9" in v for v in r.violations)

    @pytest.mark.asyncio
    async def test_g7_truncation_does_not_cause_false_g9(self, runner):
        """Hard checks run on original message — G7 truncation must not cause G9 to fire."""
        slots = SlotValues(branch="Семёновская", group="High Heels", booking_created=True)
        # Address and dress code present in original, but address would be cut by G7
        long_prefix = "✅ Запись подтверждена! " + "Х" * 260
        msg = (long_prefix +
               " Адрес: Семёновская 30а (стеклянное здание, крайняя дверь справа)."
               " С собой: носочки либо каблуки со светлой подошвой.")
        r = await runner.check(_resp(msg), slots, ConversationPhase.POST_BOOKING)
        assert r.corrected_message is not None  # G7 must have truncated the message
        assert not any("G9" in v for v in r.violations)


# ---------------------------------------------------------------------------
# G10 — schedule_id must exist in CRM
# ---------------------------------------------------------------------------

class TestG10:
    @pytest.mark.asyncio
    async def test_known_schedule_id_passes(self, runner):
        slots = SlotValues(confirmed=True, group="High Heels", client_name="Маша",
                           client_phone="89241234567", datetime_resolved=_future_dt())
        tc = ToolCall(name="create_booking", parameters={"schedule_id": "42"})
        r = await runner.check(
            _resp(tool_calls=[tc]), slots, ConversationPhase.BOOKING,
            crm_schedule=_CRM_SCHEDULE,
        )
        assert not any("G10" in v for v in r.violations)

    @pytest.mark.asyncio
    async def test_unknown_schedule_id_blocks(self, runner):
        slots = SlotValues(confirmed=True)
        tc = ToolCall(name="create_booking", parameters={"schedule_id": "999"})
        r = await runner.check(
            _resp(tool_calls=[tc]), slots, ConversationPhase.BOOKING,
            crm_schedule=_CRM_SCHEDULE,
        )
        assert any("G10" in v for v in r.violations)

    @pytest.mark.asyncio
    async def test_no_crm_schedule_skips_g10(self, runner):
        tc = ToolCall(name="create_booking", parameters={"schedule_id": "999"})
        r = await runner.check(_resp(tool_calls=[tc]), SlotValues(), ConversationPhase.BOOKING)
        assert not any("G10" in v for v in r.violations)

    @pytest.mark.asyncio
    async def test_g10_passes_when_no_crm_schedule_rfc006(self, runner):
        """G10 should pass when crm_schedule is None (RFC-006; CRM rejects invalid schedule_id)."""
        slots = SlotValues(confirmed=True, group="High Heels", client_name="Маша",
                           client_phone="89241234567", datetime_resolved=_future_dt())
        tc = ToolCall(name="create_booking", parameters={"schedule_id": "42"})
        r = await runner.check(
            _resp(tool_calls=[tc]), slots, ConversationPhase.BOOKING,
            crm_schedule=None,
            executed_tools={"get_filtered_schedule"},
        )
        assert not any("G10" in v for v in r.violations)


# ---------------------------------------------------------------------------
# G11 — datetime in slot_updates must be in the future
# ---------------------------------------------------------------------------

class TestG11:
    @pytest.mark.asyncio
    async def test_future_datetime_passes(self, runner):
        dt = _future_dt().isoformat()
        r = await runner.check(
            _resp(slot_updates={"datetime_resolved": dt}),
            SlotValues(), ConversationPhase.COLLECTING_CONTACT,
        )
        assert not any("G11" in v for v in r.violations)

    @pytest.mark.asyncio
    async def test_past_datetime_blocks(self, runner):
        dt = _past_dt().isoformat()
        r = await runner.check(
            _resp(slot_updates={"datetime_resolved": dt}),
            SlotValues(), ConversationPhase.COLLECTING_CONTACT,
        )
        assert any("G11" in v for v in r.violations)

    @pytest.mark.asyncio
    async def test_naive_datetime_treated_as_vladivostok(self, runner):
        """Naive datetime assumed Asia/Vladivostok — must not raise TypeError."""
        naive_future = (datetime.now(_STUDIO_TZ) + timedelta(days=3)).replace(tzinfo=None)
        r = await runner.check(
            _resp(slot_updates={"datetime_resolved": naive_future.isoformat()}),
            SlotValues(), ConversationPhase.COLLECTING_CONTACT,
        )
        assert not any("G11" in v for v in r.violations)

    @pytest.mark.asyncio
    async def test_no_datetime_in_updates_skips_g11(self, runner):
        r = await runner.check(
            _resp(slot_updates={"branch": "Семёновская"}),
            SlotValues(), ConversationPhase.DISCOVERY,
        )
        assert not any("G11" in v for v in r.violations)


# ---------------------------------------------------------------------------
# G12 — Schedule mention requires get_filtered_schedule tool_call
# ---------------------------------------------------------------------------

class TestG12:
    @pytest.mark.asyncio
    async def test_time_without_tool_blocks(self, runner):
        r = await runner.check(
            _resp("Ближайшее занятие в 19:00"),
            SlotValues(), ConversationPhase.SCHEDULE,
        )
        assert any("G12" in v for v in r.violations)

    @pytest.mark.asyncio
    async def test_weekday_without_tool_blocks(self, runner):
        r = await runner.check(
            _resp("Приходи в пятницу"),
            SlotValues(), ConversationPhase.SCHEDULE,
        )
        assert any("G12" in v for v in r.violations)

    @pytest.mark.asyncio
    async def test_time_with_tool_passes(self, runner):
        tc = ToolCall(name="get_filtered_schedule", parameters={"style": "High Heels"})
        r = await runner.check(
            _resp("Ближайшее занятие в 19:00", tool_calls=[tc]),
            SlotValues(), ConversationPhase.SCHEDULE, crm_schedule=_CRM_SCHEDULE,
        )
        assert not any("G12" in v for v in r.violations)

    @pytest.mark.asyncio
    async def test_no_time_no_tool_passes(self, runner):
        r = await runner.check(
            _resp("Отлично, записываю тебя!"),
            SlotValues(), ConversationPhase.CONFIRMATION,
        )
        assert not any("G12" in v for v in r.violations)

    @pytest.mark.asyncio
    async def test_post_booking_time_mention_passes(self, runner):
        """POST_BOOKING: time mention is from confirmed slot data, not hallucination."""
        slots = SlotValues(booking_created=True)
        r = await runner.check(
            _resp("Твоя запись в пятницу в 19:00. Адрес: Семёновская 30а."),
            slots, ConversationPhase.POST_BOOKING,
        )
        assert not any("G12" in v for v in r.violations)


# ---------------------------------------------------------------------------
# Clean response passes all guardrails
# ---------------------------------------------------------------------------

class TestPassingResponse:
    @pytest.mark.asyncio
    async def test_clean_response_passes(self, runner):
        r = await runner.check(
            _resp("Привет! В каком филиале удобнее заниматься?"),
            SlotValues(), ConversationPhase.GREETING,
        )
        assert r.passed
        assert r.violations == []
        assert r.corrected_message is None

    @pytest.mark.asyncio
    async def test_result_model_defaults(self, runner):
        result = GuardrailResult(passed=True)
        assert result.violations == []
        assert result.corrected_message is None


# ---------------------------------------------------------------------------
# Helper (mirrors guardrails._is_emoji for test assertions)
# ---------------------------------------------------------------------------

def _is_emoji_char(char: str) -> bool:
    cp = ord(char)
    return (
        0x1F600 <= cp <= 0x1F64F or 0x1F300 <= cp <= 0x1F5FF
        or 0x1F680 <= cp <= 0x1F6FF or 0x1F900 <= cp <= 0x1F9FF
        or 0x2600 <= cp <= 0x26FF or 0x2700 <= cp <= 0x27BF
        or 0xFE00 <= cp <= 0xFE0F or 0x1FA00 <= cp <= 0x1FA6F
        or 0x1FA70 <= cp <= 0x1FAFF
    )
