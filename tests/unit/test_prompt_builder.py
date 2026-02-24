"""Unit tests for PromptBuilder (RFC-003 §5).

Tests use anchor keywords, not full phrase matching.
"""

import pytest

from app.core.prompt_builder import LLMResponse, PromptBuilder, ToolCall
from app.core.slot_tracker import ConversationPhase
from app.knowledge.base import KnowledgeBase, BookingBranch
from app.models import SlotValues


# ---------------------------------------------------------------------------
# Minimal KB fixture (reuses pattern from test_kb_extensions)
# ---------------------------------------------------------------------------

_SERVICES = [
    {
        "id": "high-heels",
        "name": "High Heels",
        "description": "Танцы на каблуках, женственность и уверенность",
        "price_single": 900,
        "aliases": ["хай хилс", "high heels"],
    },
    {
        "id": "girly-hiphop",
        "name": "Girly Hip-Hop",
        "description": "Женственный хип-хоп в кроссовках",
        "price_single": 900,
        "aliases": ["герли", "girly hip hop"],
    },
]

_BRANCHES = [
    {
        "id": "semenovskaya",
        "name": "Семёновская",
        "address": "Семёновская 30а (стеклянное здание, крайняя дверь справа)",
        "styles": ["high-heels", "girly-hiphop"],
    },
    {
        "id": "gogolya",
        "name": "Гоголя",
        "address": "Красного Знамени 59, 8 этаж",
        "styles": ["high-heels"],
    },
]

_DRESS_CODE = {
    "high-heels": "носочки либо каблуки со светлой подошвой",
    "girly-hiphop": "кроссовки",
}

_SUBSCRIPTIONS = [
    {"name": "Разовое занятие (1 час)", "price": 900, "classes": 1, "validity_days": 1, "category": "разовые"},
    {"name": "4 занятия", "price": 3900, "classes": 4, "validity_days": 30, "category": "групповые"},
    {"name": "8 занятий", "price": 5100, "classes": 8, "validity_days": 30, "category": "групповые"},
]


@pytest.fixture
def kb() -> KnowledgeBase:
    return KnowledgeBase(
        schema_version="1.0",
        studio={
            "name": "She Dance",
            "schedule": "Расписание через CRM",
            "timezone": "Asia/Vladivostok",
            "branches": [{"name": "Тест", "address": "Тест, 1", "phone": "+7 000", "halls": []}],
        },
        tone={"style": "friendly", "pronouns": "ты"},
        services=_SERVICES,
        teachers=[{"id": "t1", "name": "Катя", "styles": ["High Heels"], "specialization": "Преп.", "aliases": []}],
        escalation={"triggers": ["жалоба"]},
        branches=_BRANCHES,
        dress_code=_DRESS_CODE,
        style_recommendations={"feminine_heels": ["high-heels"], "feminine_sneakers": ["girly-hiphop"]},
        subscriptions=_SUBSCRIPTIONS,
    )


@pytest.fixture
def builder(kb: KnowledgeBase) -> PromptBuilder:
    return PromptBuilder(kb)


def _prompt(builder: PromptBuilder, slots: SlotValues | None = None, phase: ConversationPhase = ConversationPhase.GREETING) -> str:
    return builder.build_system_prompt(slots or SlotValues(), phase)


# ---------------------------------------------------------------------------
# LLMResponse / ToolCall models
# ---------------------------------------------------------------------------

class TestModels:
    def test_llm_response_defaults(self):
        r = LLMResponse(message="Привет!")
        assert r.intent == "continue"
        assert r.slot_updates == {}
        assert r.tool_calls == []

    def test_tool_call_defaults(self):
        t = ToolCall(name="get_filtered_schedule")
        assert t.parameters == {}

    def test_llm_response_with_tool_call(self):
        r = LLMResponse(
            message="Смотрю расписание",
            tool_calls=[ToolCall(name="get_filtered_schedule", parameters={"style": "High Heels"})],
            intent="continue",
        )
        assert r.tool_calls[0].name == "get_filtered_schedule"
        assert r.tool_calls[0].parameters["style"] == "High Heels"


# ---------------------------------------------------------------------------
# All 10 sales rules present (anchor keyword checks)
# ---------------------------------------------------------------------------

class TestSalesRules:
    def test_rule_1_one_question(self, builder):
        p = _prompt(builder)
        assert "один вопрос" in p.lower()

    def test_rule_2_collection_order(self, builder):
        p = _prompt(builder)
        assert "филиал" in p.lower()

    def test_rule_3_direction_help_heels(self, builder):
        p = _prompt(builder)
        assert "каблук" in p.lower()

    def test_rule_3_direction_help_sneakers(self, builder):
        p = _prompt(builder)
        assert "кроссовк" in p.lower()

    def test_rule_4_schedule_filter(self, builder):
        p = _prompt(builder)
        assert "get_filtered_schedule" in p

    def test_rule_5_specific_date(self, builder):
        p = _prompt(builder)
        # Rule 5: "предлагай конкретную дату" — anchors from the rule itself
        assert "запишем" in p.lower() or ("пробн" in p.lower() and "дат" in p.lower())

    def test_rule_6_after_booking_address_dresscode(self, builder):
        p = _prompt(builder)
        assert "адрес" in p.lower()
        assert "dress_code" in p.lower() or "dress code" in p.lower() or "взять" in p.lower()

    def test_rule_7_no_spots(self, builder):
        p = _prompt(builder)
        assert "мест" in p.lower()

    def test_rule_8_hesitation(self, builder):
        p = _prompt(builder)
        assert "сомнева" in p.lower() or "подумаю" in p.lower() or "смущает" in p.lower()

    def test_rule_9_no_repetition(self, builder):
        p = _prompt(builder)
        assert "повтор" in p.lower() or "расписани" in p.lower()

    def test_rule_10_escalation(self, builder):
        p = _prompt(builder)
        assert "эскалац" in p.lower() or "администратор" in p.lower()


# ---------------------------------------------------------------------------
# Slots context injection
# ---------------------------------------------------------------------------

class TestSlotsContext:
    def test_phase_in_prompt(self, builder):
        p = _prompt(builder, phase=ConversationPhase.DISCOVERY)
        assert "discovery" in p

    def test_collected_group_in_prompt(self, builder):
        slots = SlotValues(group="High Heels")
        p = builder.build_system_prompt(slots, ConversationPhase.DISCOVERY)
        assert "High Heels" in p

    def test_missing_slots_listed(self, builder):
        slots = SlotValues(group="High Heels")
        p = builder.build_system_prompt(slots, ConversationPhase.DISCOVERY)
        assert "missing" in p
        assert "branch" in p

    def test_flags_in_prompt(self, builder):
        slots = SlotValues(schedule_shown=True)
        p = builder.build_system_prompt(slots, ConversationPhase.COLLECTING_CONTACT)
        assert "schedule_shown" in p

    def test_confirmed_flag_in_prompt(self, builder):
        slots = SlotValues(confirmed=True, group="High Heels", branch="Семёновская")
        p = builder.build_system_prompt(slots, ConversationPhase.BOOKING)
        assert "confirmed" in p


# ---------------------------------------------------------------------------
# KB context — branch address injection
# ---------------------------------------------------------------------------

class TestKbContext:
    def test_branch_address_when_branch_set(self, builder):
        slots = SlotValues(branch="Семёновская", group="High Heels")
        p = builder.build_system_prompt(slots, ConversationPhase.SCHEDULE)
        assert "30а" in p

    def test_all_branch_addresses_when_branch_missing(self, builder):
        """When no branch is selected, all branch addresses are injected so LLM can answer."""
        slots = SlotValues(group="High Heels")
        p = builder.build_system_prompt(slots, ConversationPhase.DISCOVERY)
        # All known branches from the test KB fixture should appear
        assert "Семёновская" in p
        assert "30а" in p

    def test_branch_address_in_discovery_without_group(self, builder):
        slots = SlotValues(branch="Семёновская")  # branch set, no group yet
        p = builder.build_system_prompt(slots, ConversationPhase.DISCOVERY)
        assert "30а" in p  # address injected even without group

    def test_dress_code_when_group_set(self, builder):
        slots = SlotValues(branch="Семёновская", group="High Heels")
        p = builder.build_system_prompt(slots, ConversationPhase.COLLECTING_CONTACT)
        assert "каблуки" in p.lower() or "носочки" in p.lower()

    def test_style_recommendations_in_discovery_without_group(self, builder):
        slots = SlotValues(branch="Семёновская")
        p = builder.build_system_prompt(slots, ConversationPhase.DISCOVERY)
        assert "feminine_heels" in p or "подбор" in p.lower()

    def test_all_services_listed_in_discovery(self, builder):
        """All directions including Vogue must appear in DISCOVERY prompt — no omissions."""
        slots = SlotValues()
        p = builder.build_system_prompt(slots, ConversationPhase.DISCOVERY)
        # Every service from the test KB fixture must be present
        for svc in builder._kb.services:
            assert svc.name in p, f"Service '{svc.name}' missing from DISCOVERY prompt"

    def test_all_services_listed_in_greeting(self, builder):
        """All directions must appear in GREETING prompt too."""
        slots = SlotValues()
        p = builder.build_system_prompt(slots, ConversationPhase.GREETING)
        for svc in builder._kb.services:
            assert svc.name in p, f"Service '{svc.name}' missing from GREETING prompt"

    def test_prices_present_in_greeting(self, builder):
        p = _prompt(builder, phase=ConversationPhase.GREETING)
        assert "₽" in p

    def test_prices_present_in_discovery(self, builder):
        slots = SlotValues(group="High Heels")
        p = builder.build_system_prompt(slots, ConversationPhase.DISCOVERY)
        assert "₽" in p

    def test_branch_address_and_dresscode_in_post_booking(self, builder):
        slots = SlotValues(
            branch="Семёновская",
            group="High Heels",
            booking_created=True,
        )
        p = builder.build_system_prompt(slots, ConversationPhase.POST_BOOKING)
        assert "30а" in p                                          # real address from KB
        assert "носочки" in p.lower() or "каблуки" in p.lower()   # real dress code from KB


# ---------------------------------------------------------------------------
# Tools section
# ---------------------------------------------------------------------------

class TestToolsSection:
    def test_get_filtered_schedule_present(self, builder):
        assert "get_filtered_schedule" in _prompt(builder)

    def test_search_kb_present(self, builder):
        assert "search_kb" in _prompt(builder)

    def test_create_booking_present(self, builder):
        assert "create_booking" in _prompt(builder)

    def test_start_cancel_flow_present(self, builder):
        assert "start_cancel_flow" in _prompt(builder)

    def test_escalate_to_admin_present(self, builder):
        assert "escalate_to_admin" in _prompt(builder)


# ---------------------------------------------------------------------------
# Constraints section
# ---------------------------------------------------------------------------

class TestConstraints:
    def test_no_invent_schedule(self, builder):
        p = _prompt(builder)
        assert "придумывай" in p.lower() or "не придумывай" in p.lower()

    def test_no_compare_teachers(self, builder):
        p = _prompt(builder)
        assert "преподавател" in p.lower()

    def test_max_length_mentioned(self, builder):
        p = _prompt(builder)
        assert "300" in p


# ---------------------------------------------------------------------------
# Response format instruction
# ---------------------------------------------------------------------------

class TestResponseFormat:
    def test_json_format_instruction_present(self, builder):
        p = _prompt(builder)
        assert "json" in p.lower()
        assert "message" in p
        assert "slot_updates" in p
        assert "intent" in p


# ---------------------------------------------------------------------------
# Prompt length (token budget guard)
# ---------------------------------------------------------------------------

class TestPromptLength:
    def test_greeting_prompt_under_4000_tokens(self, builder):
        p = _prompt(builder, phase=ConversationPhase.GREETING)
        estimated_tokens = len(p) / 4
        assert estimated_tokens < 4000, f"Prompt too long: ~{estimated_tokens:.0f} tokens"

    def test_full_slots_prompt_under_4000_tokens(self, builder):
        from datetime import datetime, timezone
        slots = SlotValues(
            branch="Семёновская",
            group="High Heels",
            experience="новичок",
            client_name="Маша Иванова",
            client_phone="89241234567",
            datetime_resolved=datetime(2026, 3, 1, 19, 0, tzinfo=timezone.utc),
            schedule_shown=True,
        )
        p = builder.build_system_prompt(slots, ConversationPhase.CONFIRMATION)
        estimated_tokens = len(p) / 4
        assert estimated_tokens < 4000, f"Prompt too long: ~{estimated_tokens:.0f} tokens"

    def test_prompt_with_schedule_data_under_4000_tokens(self, builder):
        schedule = [
            {"id": 42, "date": "2026-02-28", "time": "19:00", "group": "High Heels", "teacher": "Катя", "available_spots": 3},
            {"id": 43, "date": "2026-03-04", "time": "19:00", "group": "High Heels", "teacher": "Катя", "available_spots": 5},
        ]
        slots = SlotValues(branch="Семёновская", group="High Heels")
        p = builder.build_system_prompt(slots, ConversationPhase.SCHEDULE, schedule_data=schedule)
        estimated_tokens = len(p) / 4
        assert estimated_tokens < 4000, f"Prompt too long: ~{estimated_tokens:.0f} tokens"


# ---------------------------------------------------------------------------
# Conversation history injection (Fix 1)
# ---------------------------------------------------------------------------

class TestConversationHistory:
    def test_history_injected_when_messages_present(self, builder):
        slots = SlotValues(messages=[
            {"role": "user", "content": "Хочу на High Heels"},
            {"role": "assistant", "content": "Отлично! В каком филиале?"},
        ])
        p = builder.build_system_prompt(slots, ConversationPhase.DISCOVERY)
        assert "ИСТОРИЯ ДИАЛОГА" in p
        assert "High Heels" in p

    def test_no_history_section_when_empty(self, builder):
        p = _prompt(builder)
        assert "ИСТОРИЯ ДИАЛОГА" not in p

    def test_only_last_10_messages(self, builder):
        messages = [{"role": "user", "content": f"msg {i}"} for i in range(15)]
        slots = SlotValues(messages=messages)
        p = builder.build_system_prompt(slots, ConversationPhase.DISCOVERY)
        assert "msg 14" in p    # last message present (index 14)
        assert "msg 0" not in p  # first 5 messages trimmed (last 10 = msg 5..14)

    def test_user_role_labelled_as_klient(self, builder):
        slots = SlotValues(messages=[{"role": "user", "content": "тест"}])
        p = builder.build_system_prompt(slots, ConversationPhase.DISCOVERY)
        assert "Клиент: тест" in p

    def test_assistant_role_labelled_as_bot(self, builder):
        slots = SlotValues(messages=[{"role": "assistant", "content": "ответ бота"}])
        p = builder.build_system_prompt(slots, ConversationPhase.DISCOVERY)
        assert "Бот: ответ бота" in p

    def test_history_with_fewer_than_10_messages(self, builder):
        """All messages kept when total < 10."""
        messages = [{"role": "user", "content": f"msg {i}"} for i in range(3)]
        slots = SlotValues(messages=messages)
        p = builder.build_system_prompt(slots, ConversationPhase.DISCOVERY)
        assert "msg 0" in p  # first message present
        assert "msg 2" in p  # last message present


# ---------------------------------------------------------------------------
# _format_schedule dict formatting (Fix 3)
# ---------------------------------------------------------------------------

class TestScheduleFormatting:
    def test_dict_entry_formatted_with_id(self, builder):
        schedule = [{"id": 42, "date": "2026-02-28", "time": "19:00",
                     "group": "High Heels", "teacher": "Катя", "available_spots": 3}]
        slots = SlotValues(branch="Семёновская", group="High Heels")
        p = builder.build_system_prompt(slots, ConversationPhase.SCHEDULE, schedule_data=schedule)
        assert "id=42" in p
        assert "19:00" in p
        assert "Катя" in p

    def test_string_entry_falls_back_gracefully(self, builder):
        schedule = ["High Heels | Катя | Пт 19:00 | id=42"]
        slots = SlotValues(branch="Семёновская", group="High Heels")
        p = builder.build_system_prompt(slots, ConversationPhase.SCHEDULE, schedule_data=schedule)
        assert "id=42" in p

    def test_empty_schedule_returns_no_header(self, builder):
        slots = SlotValues(branch="Семёновская", group="High Heels")
        p = builder.build_system_prompt(slots, ConversationPhase.SCHEDULE, schedule_data=[])
        # "id=" is the formatting artifact produced for every schedule entry — absent means no entries rendered
        assert "id=" not in p


# ---------------------------------------------------------------------------
# GREETING includes style_recommendations (Fix 2)
# ---------------------------------------------------------------------------

class TestGreetingKbContext:
    def test_style_recommendations_in_greeting(self, builder):
        p = _prompt(builder, phase=ConversationPhase.GREETING)
        assert "feminine_heels" in p or "подбор" in p.lower()

    def test_prices_in_greeting(self, builder):
        p = _prompt(builder, phase=ConversationPhase.GREETING)
        assert "₽" in p

    def test_no_branch_address_in_greeting_even_if_set(self, builder):
        # branch is explicitly set — GREETING must still suppress the address
        slots = SlotValues(branch="Семёновская")
        p = builder.build_system_prompt(slots, ConversationPhase.GREETING)
        assert "30а" not in p
