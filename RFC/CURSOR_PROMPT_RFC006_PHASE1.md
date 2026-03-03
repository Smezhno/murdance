# Cursor Prompt — RFC-006 Phase 1: KBRetriever + убрать schedule из system prompt

## Контекст

System prompt содержит ~262k символов из-за `_format_schedule(crm_schedule)` в `build_system_prompt()`. 
Для объектов Schedule срабатывает `else: lines.append(f"  {entry}")` — это `str(Schedule)` с nested group/branch/sticker.
211 таких строк = 200-400k chars → LLM 400 (107k tokens при лимите 32k).

Вторая проблема: `_format_conversation_history()` в system prompt дублирует messages.

Третья: "Давай" не в `_CONFIRM_YES` → идёт в LLM вместо fast path.

## Задача

Создать KBRetriever, интегрировать в prompt_builder, убрать schedule/history из system prompt.

## STEP 1: Создать app/knowledge/retriever.py

Новый файл, ~200 строк.

```python
"""KB Retriever — phase/slot-based context injection for LLM prompt.

Replaces full KB dump with relevant excerpts.
Target: < 800 tokens per retrieval. Hard cap: 3200 chars.
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pymorphy3

if TYPE_CHECKING:
    from app.knowledge.base import FAQ, KnowledgeBase
    from app.core.slot_tracker import ConversationPhase
    from app.models import SlotValues

_MINIMAL_FALLBACK = "Студия She Dance, Владивосток. Уточните у администратора."
MAX_CONTEXT_CHARS = 3200

_STOP_WORDS = frozenset({
    "я", "ты", "мы", "вы", "он", "она", "оно", "они",
    "и", "в", "на", "с", "к", "по", "из", "за", "о", "об",
    "не", "ни", "но", "а", "да", "нет", "ли", "бы", "же",
    "что", "как", "где", "когда", "кто", "чем", "это",
    "мне", "мой", "свой", "тебе", "его", "её",
    "хочу", "можно", "нужно", "есть", "будет",
    "очень", "ещё", "еще", "уже", "тоже", "только",
})


@dataclass
class KBContext:
    text: str
    token_estimate: int
    sources: list[str] = field(default_factory=list)


class KBRetriever:
    def __init__(self, kb: "KnowledgeBase"):
        self._kb = kb
        self._morph = pymorphy3.MorphAnalyzer()
        self._faq_lemmas: list[tuple[set[str], "FAQ"]] = [
            (self._lemmatize(faq.q), faq) for faq in self._kb.faq
        ]
        # Pre-lemmatize trigger keywords
        self._sub_triggers = {self._lemma(w) for w in 
            ["абонемент", "подписка", "безлимит", "пакет", "занятие"]}
        self._policy_triggers = {self._lemma(w) for w in
            ["отмена", "перенос", "опоздание", "возврат", "надеть", "взять", "принести"]}

    def _lemma(self, word: str) -> str:
        parsed = self._morph.parse(word.lower().strip())
        return parsed[0].normal_form if parsed else word.lower()

    def _lemmatize(self, text: str) -> set[str]:
        words = text.lower().split()
        lemmas = set()
        for w in words:
            clean = w.strip(".,!?;:()\"'-")
            if not clean or clean in _STOP_WORDS:
                continue
            parsed = self._morph.parse(clean)
            if parsed:
                lemmas.add(parsed[0].normal_form)
        return lemmas

    def retrieve(
        self,
        user_text: str,
        phase: "ConversationPhase",
        slots: "SlotValues",
    ) -> KBContext:
        sections: list[tuple[int, str, str]] = []  # (priority, text, source)

        # P0: Studio name
        sections.append((0, f"Студия: {self._kb.studio.name}", "studio"))

        # P1: Slot context (filled slots = highest priority)
        self._add_slot_context(sections, slots)

        # P2: Phase context (styles/branches if not selected)
        self._add_phase_context(sections, phase, slots)

        # P3: FAQ (lemmatized search)
        self._add_faq_context(sections, user_text, phase)

        # P4: Intent triggers (subscriptions, policies)
        user_lemmas = self._lemmatize(user_text)
        self._add_intent_context(sections, user_lemmas)

        # P5: Holiday (GREETING only)
        self._add_holiday_context(sections, phase)

        return self._assemble(sections)

    def _add_slot_context(self, sections, slots):
        # Branch address
        if slots.branch:
            addr = None
            if hasattr(self._kb, "get_branch_address"):
                addr = self._kb.get_branch_address(slots.branch)
            if addr:
                sections.append((1, f"Филиал {slots.branch}: {addr}", f"branch.{slots.branch}"))

        # Style price + dress code
        if slots.group:
            svc = self._find_service(slots.style_id, slots.group)
            if svc:
                price = f"{svc.price_single}₽" if svc.price_single else "уточняется"
                sections.append((1, f"{svc.name}: разовое {price}", f"service.{svc.id}"))
            if hasattr(self._kb, "get_dress_code"):
                dress = self._kb.get_dress_code(slots.group)
                if dress:
                    sections.append((2, f"Что надеть: {dress}", f"dress.{slots.group}"))

        # Teacher info
        if slots.teacher:
            teacher = self._find_teacher(slots.teacher_id, slots.teacher)
            if teacher:
                sections.append((2, f"{teacher.name}: {', '.join(teacher.styles)}", f"teacher.{teacher.id}"))

    def _add_phase_context(self, sections, phase, slots):
        from app.core.slot_tracker import ConversationPhase

        if phase in (ConversationPhase.GREETING, ConversationPhase.DISCOVERY):
            if not slots.group:
                styles = [f"{s.name}: {s.description[:50]}" for s in self._kb.services]
                sections.append((2, "Направления:\n" + "\n".join(f"- {s}" for s in styles), "services"))
            if not slots.branch and hasattr(self._kb, "branches") and self._kb.branches:
                names = [b.name for b in self._kb.branches[:6]]
                sections.append((2, f"Филиалы: {', '.join(names)}", "branches"))

    def _add_faq_context(self, sections, user_text, phase):
        from app.core.slot_tracker import ConversationPhase
        # No FAQ in late booking phases
        if phase in (ConversationPhase.CONFIRMATION, ConversationPhase.BOOKING,
                      ConversationPhase.POST_BOOKING):
            return

        query_lemmas = self._lemmatize(user_text)
        if not query_lemmas:
            return

        scored = []
        for faq_lemmas, faq in self._faq_lemmas:
            if not faq_lemmas:
                continue
            overlap = len(query_lemmas & faq_lemmas)
            if overlap > 0:
                score = overlap / min(len(query_lemmas), len(faq_lemmas))
                scored.append((score, faq))

        scored.sort(key=lambda x: -x[0])
        for score, faq in scored[:2]:
            if score >= 0.3:
                sections.append((3, f"FAQ: {faq.q}\n→ {faq.a}", "faq"))

    def _add_intent_context(self, sections, user_lemmas):
        if user_lemmas & self._sub_triggers and self._kb.subscriptions:
            lines = [f"- {s.name}: {s.classes} зан., {s.price}₽" 
                     for s in self._kb.subscriptions[:5]]
            sections.append((4, "Абонементы:\n" + "\n".join(lines), "subscriptions"))

        if user_lemmas & self._policy_triggers and self._kb.policies:
            p = self._kb.policies
            parts = []
            if p.cancellation: parts.append(f"Отмена: {p.cancellation[:100]}")
            if p.trial_class: parts.append(f"Пробное: {p.trial_class[:100]}")
            if p.what_to_bring: parts.append(f"С собой: {p.what_to_bring[:100]}")
            if parts:
                sections.append((4, "\n".join(parts), "policies"))

    def _add_holiday_context(self, sections, phase):
        from app.core.slot_tracker import ConversationPhase
        if phase != ConversationPhase.GREETING:
            return
        from datetime import date
        today = date.today().isoformat()
        for h in self._kb.holidays:
            if h.from_date <= today <= h.to_date:
                sections.append((1, f"⚠️ {h.name}: {h.message}", "holiday"))
                break

    def _assemble(self, sections) -> KBContext:
        sections.sort(key=lambda x: x[0])
        parts, sources = [], []
        total = 0
        for priority, text, source in sections:
            text_len = len(text)
            if total + text_len > MAX_CONTEXT_CHARS:
                if priority <= 1:
                    remaining = MAX_CONTEXT_CHARS - total
                    if remaining > 100:
                        parts.append(text[:remaining])
                        sources.append(source + ".truncated")
                        total += remaining
                continue
            parts.append(text)
            sources.append(source)
            total += text_len

        text = "\n\n".join(parts) if parts else _MINIMAL_FALLBACK
        if not parts:
            sources = ["fallback"]
        return KBContext(text=text, token_estimate=len(text) // 4, sources=sources)

    def _find_service(self, style_id, group_name):
        if style_id is not None:
            for s in self._kb.services:
                if str(s.id) == str(style_id):
                    return s
        for s in self._kb.services:
            if s.name.lower() == group_name.lower():
                return s
        return None

    def _find_teacher(self, teacher_id, teacher_name):
        if teacher_id is not None:
            for t in self._kb.teachers:
                if str(t.id) == str(teacher_id):
                    return t
        for t in self._kb.teachers:
            if t.name.lower() == teacher_name.lower():
                return t
        return None
```

## STEP 2: Изменить prompt_builder.py

### 2a: Добавить KBRetriever в конструктор

```python
class PromptBuilder:
    def __init__(self, kb: "KnowledgeBase"):
        self._kb = kb
        # ADD:
        from app.knowledge.retriever import KBRetriever
        self._retriever = KBRetriever(kb)
```

### 2b: Изменить сигнатуру build_system_prompt

```python
    def build_system_prompt(
        self,
        slots: "SlotValues",
        phase: "ConversationPhase",
        schedule_data: list | None = None,  # DEPRECATED, kept for compatibility
        user_text: str = "",                 # NEW: for RAG FAQ search
    ) -> str:
```

### 2c: Заменить содержимое build_system_prompt

Найти текущую сборку sections. Заменить на:

```python
        sections = [
            self._role_and_tone(),
            self._sales_rules(),
            self._format_slots_context(slots, phase),
            self._retriever.retrieve(user_text, phase, slots).text,
            self._format_tools(),
            self._constraints(),
        ]
        # REMOVED: _format_conversation_history — already in messages via _build_messages
        # REMOVED: _format_schedule — schedule only via tool call get_filtered_schedule
        # REMOVED: _format_kb_context — replaced by KBRetriever
        return "\n\n".join(s for s in sections if s)
```

### 2d: Add tool truth to _constraints()

In `_constraints()` method, add this rule:

```
- Если tool (get_filtered_schedule, search_kb) вернул данные — используй данные из tool, 
  а не из системного контекста. Tool output = source of truth для расписания и наличия мест.
```

### 2e: DO NOT delete old methods yet

Keep `_format_conversation_history`, `_format_schedule`, `_format_kb_context` 
as dead code for now. We'll clean up after verification. This prevents breaking
anything if we need to rollback.

## STEP 3: Изменить engine.py

### 3a: Передать user_text в build_system_prompt

Find where `build_system_prompt` is called in `_llm_loop()`:

```python
# BEFORE (something like):
system_prompt = self._pb.build_system_prompt(slots, phase, crm_schedule)

# AFTER:
system_prompt = self._pb.build_system_prompt(
    slots, phase, 
    schedule_data=None,      # Schedule ONLY via tool call
    user_text=message.text,  # For RAG FAQ search
)
print(f"SYSTEM_PROMPT_SIZE: {len(system_prompt)} chars")
```

Also: if there's code that fetches crm_schedule ONLY for build_system_prompt
(not for tool calls), remove that fetch. Schedule fetching should only happen
inside tool call execution (get_filtered_schedule).

### 3b: Расширить _CONFIRM_YES

```python
_CONFIRM_YES = {
    "да", "yes", "ок", "ok", "+",
    "подтверждаю", "подтверждаем",
    "давай", "конечно", "запиши", "записывай",
    "хочу", "go", "ага", "угу", "давайте",
}
```

### 3c: Tool results summary

Find where tool results are appended to messages (in _llm_loop or _execute_tool_calls).
Add summarization:

```python
def _summarize_tool_result(tool_name: str, result_text: str) -> str:
    """Compact tool result for LLM context. Keeps first 500 chars."""
    if len(result_text) <= 500:
        return result_text
    # Keep header + first entries
    lines = result_text.strip().split("\n")
    kept = lines[:6]
    if len(lines) > 6:
        kept.append(f"(и ещё {len(lines) - 6} строк)")
    summary = "\n".join(kept)
    return summary[:500]
```

Apply where tool results become messages:
```python
# BEFORE:
messages.append({"role": "user", "content": f"[{tool_name}]: {result_text}"})

# AFTER:
capped = _summarize_tool_result(tool_name, result_text)
messages.append({"role": "user", "content": f"[{tool_name}]: {capped}"})
```

## STEP 4: Инициализация в main.py

Check that KBRetriever is initialized correctly when PromptBuilder is created.
Since KBRetriever is created INSIDE PromptBuilder.__init__, no changes needed
in main.py. But verify:

```python
# In main.py or wherever PromptBuilder is instantiated:
pb = PromptBuilder(kb)  # KBRetriever created automatically
```

Ensure `pymorphy3` is in requirements/dependencies. It should already be there
(used by entity resolver). If not: `pip install pymorphy3`.

## STEP 5: Unit tests

Create `tests/unit/test_kb_retriever.py`:

```python
"""Tests for KBRetriever."""

import pytest
from unittest.mock import MagicMock
from app.knowledge.retriever import KBRetriever, KBContext, MAX_CONTEXT_CHARS

# Test 1: retrieve returns < MAX_CONTEXT_CHARS
def test_retrieve_under_char_limit(mock_kb, mock_slots_empty):
    retriever = KBRetriever(mock_kb)
    from app.core.slot_tracker import ConversationPhase
    ctx = retriever.retrieve("Привет", ConversationPhase.GREETING, mock_slots_empty)
    assert len(ctx.text) <= MAX_CONTEXT_CHARS
    assert ctx.token_estimate <= MAX_CONTEXT_CHARS // 4

# Test 2: fallback when no relevant context
def test_retrieve_fallback(mock_kb_empty, mock_slots_empty):
    retriever = KBRetriever(mock_kb_empty)
    ctx = retriever.retrieve("", ConversationPhase.GREETING, mock_slots_empty)
    assert ctx.text  # Not empty
    assert "fallback" in ctx.sources

# Test 3: slot context injected when branch/style set
def test_slot_context_injected(mock_kb, mock_slots_with_branch):
    retriever = KBRetriever(mock_kb)
    ctx = retriever.retrieve("Запиши", ConversationPhase.SCHEDULE, mock_slots_with_branch)
    assert "Гоголя" in ctx.text or "branch" in str(ctx.sources)

# Test 4: FAQ lemmatization works
def test_faq_lemmatization(mock_kb_with_faq):
    retriever = KBRetriever(mock_kb_with_faq)
    # "отменить" should match FAQ about "отмена"
    ctx = retriever.retrieve("как отменить занятие", ConversationPhase.GREETING, mock_slots_empty)
    assert "faq" in str(ctx.sources)

# Test 5: styles listed in GREETING when group not set
def test_styles_in_greeting(mock_kb, mock_slots_empty):
    retriever = KBRetriever(mock_kb)
    ctx = retriever.retrieve("что есть?", ConversationPhase.GREETING, mock_slots_empty)
    assert "services" in str(ctx.sources)

# Test 6: no FAQ in CONFIRMATION phase
def test_no_faq_in_confirmation(mock_kb_with_faq, mock_slots_empty):
    retriever = KBRetriever(mock_kb_with_faq)
    ctx = retriever.retrieve("отмена", ConversationPhase.CONFIRMATION, mock_slots_empty)
    assert "faq" not in str(ctx.sources)
```

Create appropriate fixtures (mock_kb, mock_slots_empty, etc.) that match
your existing KB and SlotValues models.

## Verification

After implementation:

1. `python -m pytest tests/unit/test_kb_retriever.py -v` — all pass
2. `python -m pytest tests/ -v` — no regressions
3. Deploy to dev/staging
4. Send: /start → "Хочу к Тане на хиллс" → watch logs:
   - `SYSTEM_PROMPT_SIZE: <6000` (was 262000)
   - No LLM 400 errors
5. Send: "Давай" after schedule shown → fast path, no LLM call
6. Check llm_calls table: prompt_tokens < 5000

## DO NOT

- Do NOT delete _format_conversation_history, _format_schedule, _format_kb_context yet
- Do NOT change schedule_flow.py or adapter.py in this phase
- Do NOT add sentence-transformers dependency
- Do NOT change guardrails.py in this phase (Phase 2)
- Do NOT remove the _build_messages 80k char cap (keep as safety net)
