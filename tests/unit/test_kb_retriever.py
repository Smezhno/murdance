"""Tests for KBRetriever (RFC-006 Phase 1)."""

import pytest

from app.core.slot_tracker import ConversationPhase
from app.knowledge.base import KnowledgeBase, FAQ
from app.knowledge.retriever import KBRetriever, KBContext, MAX_CONTEXT_CHARS
from app.models import SlotValues


# ---------------------------------------------------------------------------
# Minimal KB (no branches, no FAQ) — for fallback / under-limit tests
# ---------------------------------------------------------------------------

_MINIMAL_KB = {
    "schema_version": "1.0",
    "studio": {
        "name": "She Dance",
        "schedule": "Через CRM",
        "timezone": "Asia/Vladivostok",
        "branches": [
            {"name": "Тест", "address": "Тест, 1", "phone": "+7 000", "halls": []}
        ],
    },
    "tone": {"style": "friendly", "pronouns": "ты"},
    "services": [
        {"id": "hh", "name": "High Heels", "description": "Каблуки", "price_single": 900, "aliases": []},
    ],
    "teachers": [
        {"id": "t1", "name": "Катя", "styles": ["High Heels"], "specialization": "Преп.", "aliases": []},
    ],
    "escalation": {"triggers": ["жалоба"]},
    "branches": [],
    "faq": [],
    "holidays": [],
}


@pytest.fixture
def mock_kb() -> KnowledgeBase:
    """KB with services and branches (e.g. Гоголя)."""
    return KnowledgeBase(
        **_MINIMAL_KB,
        branches=[
            {
                "id": "gogolya",
                "name": "Гоголя",
                "crm_branch_id": "XX",
                "address": "Красного Знамени 59, 8 этаж",
                "styles": ["hh"],
                "aliases": ["гоголя"],
            },
        ],
    )


@pytest.fixture
def mock_kb_empty() -> KnowledgeBase:
    """Minimal KB: studio + one service, no branches, no FAQ."""
    return KnowledgeBase(**_MINIMAL_KB)


@pytest.fixture
def mock_kb_with_faq() -> KnowledgeBase:
    """KB with FAQ about cancellation."""
    return KnowledgeBase(
        **_MINIMAL_KB,
        faq=[
            FAQ(q="Как отменить занятие?", a="Напишите за 24 часа до занятия."),
            FAQ(q="Сколько стоит пробное?", a="900 рублей."),
        ],
    )


@pytest.fixture
def mock_slots_empty() -> SlotValues:
    return SlotValues()


@pytest.fixture
def mock_slots_with_branch() -> SlotValues:
    return SlotValues(branch="Гоголя", group="High Heels")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_retrieve_under_char_limit(mock_kb: KnowledgeBase, mock_slots_empty: SlotValues) -> None:
    """retrieve() returns context under MAX_CONTEXT_CHARS."""
    retriever = KBRetriever(mock_kb)
    ctx = retriever.retrieve("Привет", ConversationPhase.GREETING, mock_slots_empty)
    assert len(ctx.text) <= MAX_CONTEXT_CHARS
    assert ctx.token_estimate <= MAX_CONTEXT_CHARS // 4


def test_retrieve_fallback(mock_kb_empty: KnowledgeBase, mock_slots_empty: SlotValues) -> None:
    """Minimal KB still returns non-empty context (studio + phase context)."""
    retriever = KBRetriever(mock_kb_empty)
    ctx = retriever.retrieve("", ConversationPhase.GREETING, mock_slots_empty)
    assert ctx.text
    assert "She Dance" in ctx.text
    assert "studio" in ctx.sources


def test_slot_context_injected(
    mock_kb: KnowledgeBase, mock_slots_with_branch: SlotValues
) -> None:
    """When branch is set, branch address appears in context."""
    retriever = KBRetriever(mock_kb)
    ctx = retriever.retrieve(
        "Запиши", ConversationPhase.SCHEDULE, mock_slots_with_branch
    )
    assert "Гоголя" in ctx.text or "branch" in str(ctx.sources)


def test_faq_lemmatization(
    mock_kb_with_faq: KnowledgeBase, mock_slots_empty: SlotValues
) -> None:
    """User 'как отменить занятие' matches FAQ about cancellation (lemmatization)."""
    retriever = KBRetriever(mock_kb_with_faq)
    ctx = retriever.retrieve(
        "как отменить занятие", ConversationPhase.GREETING, mock_slots_empty
    )
    assert "faq" in str(ctx.sources)


def test_styles_in_greeting(
    mock_kb: KnowledgeBase, mock_slots_empty: SlotValues
) -> None:
    """In GREETING without group, services list is included."""
    retriever = KBRetriever(mock_kb)
    ctx = retriever.retrieve("что есть?", ConversationPhase.GREETING, mock_slots_empty)
    assert "services" in str(ctx.sources)


def test_no_faq_in_confirmation(
    mock_kb_with_faq: KnowledgeBase, mock_slots_empty: SlotValues
) -> None:
    """FAQ is not injected in CONFIRMATION phase."""
    retriever = KBRetriever(mock_kb_with_faq)
    ctx = retriever.retrieve(
        "отмена", ConversationPhase.CONFIRMATION, mock_slots_empty
    )
    assert "faq" not in str(ctx.sources)
