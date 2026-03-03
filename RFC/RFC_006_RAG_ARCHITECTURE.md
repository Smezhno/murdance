# RFC-006: RAG-слой для DanceBot

## Проблема

System prompt содержит ~262k символов (~65k tokens) из-за:
1. `format_for_llm()` — дампит всю KB (все стили, все преподаватели, все FAQ, все цены)
2. `_format_conversation_history()` — вставляет в system prompt историю с schedule dump (211 entries)
3. Schedule tool results хранятся raw в messages history

При YandexGPT Pro input 0.24₽/1k tokens: каждый раздутый вызов стоит 15-25₽.
При нормальном промпте (~2k tokens) — 0.5₽.

## Принцип

Вместо "дай LLM всё, пусть разберётся" → "найди релевантное, дай LLM только это".

LLM остаётся умным агентом. Он по-прежнему:
- Ведёт свободный диалог
- Сам решает какие slots собирать и в каком порядке
- Формулирует ответы естественным языком
- Вызывает tool calls когда нужно

Но контекст, который он получает — компактный и релевантный.

## Архитектура

```
                 User message
                      │
                      ▼
            ┌─────────────────┐
            │ ConversationEngine│
            │                   │
            │  1. Load session  │
            │  2. compute_phase │
            │  3. retrieve()    │◄─── RAG: получить релевантный контекст
            │  4. build_prompt  │
            │  5. LLM call      │     System prompt: ~2000 tokens
            │  6. tool calls    │     (role + rules + slots + retrieved KB)
            │  7. guardrails    │
            │  8. save & reply  │
            └─────────────────┘
                      │
                      ▼
            ┌─────────────────┐
            │  RAG Retriever   │
            │                   │
            │  retrieve(        │
            │    query,         │  query = user message + current slots
            │    phase,         │  phase = текущая фаза диалога
            │    slots          │  slots = что уже заполнено
            │  ) → KBContext    │
            │                   │
            │  Стратегии:       │
            │  - phase-based    │  GREETING → services + branches
            │  - intent-based   │  FAQ вопрос → search FAQ
            │  - slot-based     │  branch set → только адрес этого филиала
            └─────────────────┘
```

## Компонент: KBRetriever

```python
# app/knowledge/retriever.py

@dataclass
class KBContext:
    """Compact KB context for injection into LLM prompt."""
    text: str            # Готовый текст для вставки в system prompt
    token_estimate: int  # Оценка размера в токенах
    sources: list[str]   # Откуда взяли (для дебага)

class KBRetriever:
    """Retrieves relevant KB context based on conversation state.
    
    Replaces format_for_llm() which dumps everything.
    Target: < 1000 tokens per retrieval.
    """
    
    def __init__(self, kb: KnowledgeBase):
        self._kb = kb
        # При старте строим индексы для быстрого поиска
        self._faq_index = self._build_faq_index()
        self._style_index = self._build_style_index()
    
    def retrieve(
        self, 
        user_text: str,
        phase: ConversationPhase,
        slots: SlotValues,
    ) -> KBContext:
        """Retrieve relevant KB context. Max ~800 tokens."""
        
        sections: list[str] = []
        sources: list[str] = []
        
        # 1. Всегда: базовая информация о студии (1 строка)
        sections.append(f"Студия: {self._kb.studio.name}")
        sources.append("studio.name")
        
        # 2. Phase-based injection
        if phase in (ConversationPhase.GREETING, ConversationPhase.STYLE_SELECTION):
            # Пользователь выбирает направление — дай список стилей (компактно)
            styles = [f"{s.name}: {s.description[:60]}" for s in self._kb.services]
            sections.append("Направления:\n" + "\n".join(f"- {s}" for s in styles))
            sources.append("services.list")
        
        # 3. Slot-based injection
        if slots.branch:
            # Филиал выбран — дай только его адрес
            branch_info = self._kb.get_branch_address(slots.branch)
            if branch_info:
                sections.append(f"Адрес {slots.branch}: {branch_info}")
                sources.append(f"branch.{slots.branch}")
        else:
            # Филиал не выбран — дай список филиалов (компактно)
            branches = self._get_branches_compact()
            if branches:
                sections.append(branches)
                sources.append("branches.list")
        
        if slots.group:
            # Стиль выбран — дай цену и dress code
            service = self._find_service(slots.group)
            if service:
                price = f"{service.price_single}₽" if service.price_single else "уточняется"
                sections.append(f"{service.name}: разовое {price}")
                sources.append(f"service.{service.id}.price")
            dress = self._kb.get_dress_code(slots.group) if hasattr(self._kb, 'get_dress_code') else None
            if dress:
                sections.append(f"Что надеть: {dress}")
                sources.append(f"dress_code.{slots.group}")
        
        # 4. FAQ search (если не booking flow)
        if phase in (ConversationPhase.GREETING, ConversationPhase.FAQ):
            faq_matches = self._search_faq(user_text, top_k=2)
            if faq_matches:
                for faq in faq_matches:
                    sections.append(f"FAQ: {faq.q}\n→ {faq.a}")
                sources.append("faq.search")
        
        # 5. Subscriptions — только если спрашивают про абонементы
        if self._mentions_subscription(user_text):
            subs = self._format_subscriptions_compact()
            if subs:
                sections.append(subs)
                sources.append("subscriptions")
        
        # 6. Policies — только если спрашивают
        if self._mentions_policy(user_text):
            policies = self._format_policies_compact()
            if policies:
                sections.append(policies)
                sources.append("policies")
        
        # 7. Holidays — только если активны
        holiday = self._get_active_holiday()
        if holiday:
            sections.append(f"⚠️ {holiday.name}: {holiday.message}")
            sources.append("holidays.active")
        
        text = "\n\n".join(sections)
        token_estimate = len(text) // 4  # rough estimate
        
        return KBContext(
            text=text,
            token_estimate=token_estimate,
            sources=sources,
        )
    
    def _search_faq(self, query: str, top_k: int = 2) -> list[FAQ]:
        """Simple keyword search in FAQ. 
        
        Phase 2: replace with embedding search for better quality.
        """
        query_lower = query.lower()
        scored = []
        for faq in self._kb.faq:
            # Score by keyword overlap
            q_words = set(faq.q.lower().split())
            overlap = len(q_words & set(query_lower.split()))
            if overlap > 0:
                scored.append((overlap, faq))
        scored.sort(key=lambda x: -x[0])
        return [faq for _, faq in scored[:top_k]]
    
    def _mentions_subscription(self, text: str) -> bool:
        keywords = {"абонемент", "подписк", "пакет", "8 занятий", "безлимит"}
        text_lower = text.lower()
        return any(k in text_lower for k in keywords)
    
    def _mentions_policy(self, text: str) -> bool:
        keywords = {"отмен", "перенос", "опоздан", "вернуть", "возврат", "что взять", "что надеть"}
        text_lower = text.lower()
        return any(k in text_lower for k in keywords)
```

## Изменения в prompt_builder.py

```python
# БЫЛО:
def _format_kb_context(self, slots, phase) -> str:
    return self._kb.format_for_llm()  # 10-20k chars ВСЕГДА

# СТАЛО:
def _format_kb_context(self, slots, phase, user_text: str = "") -> str:
    ctx = self._retriever.retrieve(user_text, phase, slots)
    return ctx.text  # 500-2000 chars, только релевантное
```

## Изменения в engine.py: schedule tool results

Текущая проблема: `get_filtered_schedule` возвращает огромный текст (211 entries),
который сохраняется в messages history → на следующем LLM call попадает в контекст.

```python
# БЫЛО (в _execute_tool_calls):
messages.append({"role": "user", "content": f"[get_filtered_schedule]: {full_schedule_text}"})
# full_schedule_text = 50-100k chars (211 entries)

# СТАЛО:
# 1. generate_schedule_response уже фильтрует и возвращает компактный текст
# 2. В history сохраняем ТОЛЬКО summary
schedule_summary = _summarize_schedule_result(schedule_text, max_entries=5)
messages.append({"role": "user", "content": f"[get_filtered_schedule]: {schedule_summary}"})

def _summarize_schedule_result(text: str, max_entries: int = 5) -> str:
    """Compact schedule for history. Full text goes to user, summary to LLM memory."""
    lines = text.strip().split("\n")
    if len(lines) <= max_entries + 1:  # header + entries
        return text
    # Keep header + first N entries + count
    kept = lines[:max_entries + 1]
    remaining = len(lines) - max_entries - 1
    kept.append(f"(и ещё {remaining} занятий)")
    return "\n".join(kept)
```

## Изменения: убрать conversation_history из system prompt

```python
# БЫЛО в build_system_prompt():
sections = [
    self._role_and_tone(),
    self._sales_rules(),
    self._format_slots_context(slots, phase),
    self._format_conversation_history(slots),  # ← 200k+ chars с schedule dump
    self._format_kb_context(slots, phase),
    self._format_tools(),
    self._constraints(),
]

# СТАЛО:
sections = [
    self._role_and_tone(),           # ~500 chars
    self._sales_rules(),             # ~2000 chars
    self._format_slots_context(slots, phase),  # ~300 chars
    self._format_kb_context(slots, phase, user_text),  # ~800 chars (RAG)
    self._format_tools(),            # ~1000 chars
    self._constraints(),             # ~500 chars
]
# Total: ~5100 chars ≈ 1300 tokens
# History отдельно в messages (managed by _build_messages с sliding window)
```

## Бюджет токенов на вызов

| Компонент | Текущий | После RAG |
|-----------|---------|-----------|
| System prompt | 65 000 tokens | **1 300 tokens** |
| History (10 msgs) | 5 000 tokens | **2 000 tokens** (sliding window + summaries) |
| User message | 100 tokens | 100 tokens |
| **Total input** | **70 000+** | **~3 400** |
| Output | 500 tokens | 500 tokens |
| **Cost per call** | 17₽ | **0.9₽** |
| **Cost per dialog (6 calls)** | 102₽ | **5.4₽** |
| **Cost 100 clients/day** | 10 200₽/день | **540₽/день** |
| **Monthly LLM cost** | 306 000₽ | **16 200₽** |

## План реализации

### Phase 1: KBRetriever (1 день)

1. Создать `app/knowledge/retriever.py` — KBRetriever class
2. Keyword search для FAQ (не embedding — это MVP)
3. Phase-based + slot-based injection
4. Unit тесты: проверить что retrieve() возвращает < 1000 tokens
5. Проверить что retrieve() включает нужные данные для каждой фазы

### Phase 2: Интеграция (1 день)

1. prompt_builder: заменить `format_for_llm()` на `retriever.retrieve()`
2. prompt_builder: убрать `_format_conversation_history()` из system prompt
3. engine.py: schedule results → summary в history
4. engine.py: `_CONFIRM_YES` — добавить "давай", "конечно" и т.д.
5. Deploy + тест полного flow

### Phase 3: Фильтрация по ID (1 день)

1. schedule_flow: фильтровать по style_id, branch_id, teacher_id вместо строк
2. engine.py: убедиться что IDs передаются в slots_dict
3. Deploy + тест "Хочу к Тане на хиллс" → "Гоголя" → schedule показывается

### Phase 4: Embedding FAQ search (позже)

1. При старте: encode all FAQ questions через sentence-transformers
2. На вопрос: encode user text → cosine similarity → top-2 FAQ
3. Это даст лучшее качество ответов на FAQ

## Метрики успеха

| Метрика | До | После | Как проверить |
|---------|-----|-------|---------------|
| System prompt tokens | 65 000 | < 2 000 | `SYSTEM_PROMPT_SIZE:` в логах |
| Total input tokens/call | 70 000+ | < 5 000 | llm_calls table |
| LLM cost/dialog | 100₽+ | < 6₽ | llm_calls table |
| LLM 400 errors | есть | 0 | errors table |
| "Давай" → booking | crash | works | manual test |
| Schedule filter match | 0/211 | >0 | `SCHEDULE_FILTER:` в логах |
| Agent quality | есть | сохраняется | prompt regression tests |

## Совместимость с CONTRACT

| CONTRACT § | Правило | Как RAG помогает |
|------------|---------|------------------|
| §5 | LLM only for NLU + rewriting | RAG не меняет роль LLM |
| §11 | Price must match KB | retrieve() инъектирует точную цену |
| §11 | No schedule invention | Tool call + ID filter, не dump |
| §12 | Budget Guard | 20x меньше tokens → далеко от лимитов |
| §15 | KB validated at start | KBRetriever использует ту же KB |
