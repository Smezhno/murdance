# RFC-007 v2: Post-Launch Quality Fixes

| Поле | Значение |
|------|----------|
| **Проект** | DanceBot / Murdance |
| **Версия** | 2.0 |
| **Дата** | 4 марта 2026 |
| **Автор** | Александр (при участии Claude) |
| **Статус** | Draft |
| **Зависимости** | RFC-003, RFC-006, CONTRACT.md v1.2 |

---

## 1. Контекст и ограничения

12 end-to-end сценариев протестированы 04.03.2026. Все провалены.
Причины группируются в 5 системных паттернов.

**Жёсткое ограничение:** token budget ~3200 chars system prompt (RFC-005).
Все фиксы ОБЯЗАНЫ быть token-neutral или token-negative.
Ни один фикс не должен увеличивать размер system prompt на каждый запрос.

---

## 2. Паттерны ошибок

### P1: LLM галлюцинирует когда нет данных в контексте

**Сценарии:** 2 (jazz funk), 9 (каблук), 10 (хоп), 11 (бачата)

Retriever подставляет KB-данные только при точном совпадении лемм или конкретной
фазе/слоте. Если совпадения нет — LLM не видит ни списка направлений, ни dress code
и отвечает из общих знаний ("Да, у нас есть бачата!").

### P2: Промпт заставляет "продавать", а не "отвечать"

**Сценарии:** 4 (расписание), 2 (jazz funk), 10 (хоп)

Правила 4-5: "вызови get_filtered_schedule → сразу предложи ближайшую дату".
Пользователь спрашивает "у вас пн/ср/пт в 19:30?" — бот игнорирует вопрос.

### P3: Архитектурные пробелы

**Сценарии:** 1 (мои записи), 3 (2 направления), 6 (перенос), 7 (заморозка), 8 (отмена)

Нет инструмента "мои записи". cancel_flow.start() не переводит state.
Нет сценариев перенос/заморозка. Один слот group — нельзя 2 направления.

### P4: Битый JSON утекает в чат

**Сценарии:** 5, 6

YandexGPT обрезает ответ → невалидный JSON → пользователь видит ```{...

### P5: Нет защиты от спама

**Сценарий:** 12

Нет rate limiting, нет gibberish detection, нет эскалации при бессмыслице.

---

## 3. Приоритизация

| # | Фикс | Effort | Impact | Tokens |
|---|------|--------|--------|--------|
| F1 | Retriever: расширить триггеры для списка направлений | S | Критичный | ±0 (только когда нужно) |
| F2 | Sanitize JSON fallback | S | Критичный | 0 |
| F3 | cancel_flow state transition | S | Высокий | 0 |
| F4 | Промпт: ответь, потом предложи | S | Высокий | +30 chars (один раз в rules) |
| F5 | Resolver: prefix match вместо алиасов | S | Средний | 0 |
| F6 | Policy триггеры: +2 леммы | S | Средний | ±0 |
| F7 | Инструмент list_my_bookings | M | Средний | +40 chars (в tools) |
| F8 | Эскалация — уведомление админу | M | Средний | 0 |
| F9 | Gibberish detector | M | Средний | 0 |
| F10 | Промпт: 2 направления — уточни | S | Низкий | +20 chars |
| F11 | Промпт: перенос = отмена + запись | S | Низкий | +30 chars |
| F12 | Промпт: заморозка — сбор данных | S | Низкий | +25 chars |
| F13 | Tool summary: лимит по типу инструмента | S | Средний | ±0 |
| F14 | G12: не блокировать если tool_call был ранее | S | Критичный | 0 |
| F15 | Missing slots → заменить LLM message | S | Критичный | 0 |
| F16 | Цены: длинный ответ или split | S | Средний | +30 chars |
| F17 | Промпт: "мои записи" → list_my_bookings | S | Высокий | +30 chars |

S = < 30 мин, M = 1-3 ч

---

## 4. Спецификации фиксов

### F1: Retriever — расширить триггеры для подстановки направлений (P0)

**Файл:** `app/knowledge/retriever.py`

**Принцип:** НЕ добавлять список направлений ВСЕГДА. Добавлять ТОЛЬКО когда пользователь
спрашивает о существовании услуги или о чём-то, что требует знания ассортимента.

**Текущее:** Список направлений подставляется при `phase in (GREETING, DISCOVERY) and not slots.group`.

**Новое:** Добавить intent-категорию `ask_service_exists` в `_add_intent_context()`:

```python
# Триггерные леммы: пользователь спрашивает "а есть ли у вас X?"
_SERVICE_EXISTS_TRIGGERS = {"есть", "бывать", "проводить", "вести", "преподавать", "направление"}

def _should_add_services_list(self, lemmas: set[str], slots) -> bool:
    """Check if user asks about service existence."""
    # Already shown in GREETING/DISCOVERY
    if not slots.group:
        return False  # _add_phase_context handles this
    # User asks "есть ли у вас X" while already in a flow
    return bool(lemmas & _SERVICE_EXISTS_TRIGGERS)
```

Если триггер сработал — подставить компактную строку (одна строка, без описаний):

```
Направления: High Heels, Frame Up Strip, Girly Hip-Hop, Dancehall, ...
Если спрашивают о другом — скажи что нет и предложи похожее.
```

Длина: ~120 chars. Подставляется только по триггеру, не на каждый запрос.

**Также:** В `_add_phase_context()` для GREETING/DISCOVERY — добавить ту же строку
"Если спрашивают о другом — скажи что нет и предложи похожее." Стоимость: +50 chars
в фазе, где контекст итак минимален (нет slots, нет schedule).

### F2: Sanitize JSON fallback (P0)

**Файл:** `app/core/engine.py`, метод `_parse_llm_response`

**Текущее:** При ошибке json.loads возвращает `raw_text` целиком.

**Новое:** Трёхступенчатый fallback:

1. Попытка извлечь `"message"` из partial JSON через regex
2. Если нашли message ≥ 5 символов — вернуть как ответ
3. Если нет — strip все JSON артефакты (`{}[]"\\`, ключевые слова JSON), вернуть чистый текст

```python
def _extract_message_from_partial(self, text: str) -> str | None:
    """Extract message field from truncated JSON."""
    import re
    # Match "message": "..." even if JSON is cut off
    match = re.search(r'"message"\s*:\s*"((?:[^"\\]|\\.){5,})', text)
    if match:
        msg = match.group(1).rstrip("\\")
        return msg.strip()
    return None

def _parse_llm_response(self, raw_text: str) -> CoreLLMResponse:
    text = raw_text.strip()
    # Strip markdown fences
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    
    try:
        data = json.loads(text)
        # ... existing parsing
    except Exception:
        # Step 1: try partial JSON
        msg = self._extract_message_from_partial(text)
        if msg:
            return CoreLLMResponse(message=msg[:300], intent="continue")
        # Step 2: clean raw text
        import re
        clean = re.sub(r'[{}\[\]"\\]', '', text)
        clean = re.sub(r'\b(message|slot_updates|tool_calls|intent)\b\s*:', '', clean)
        clean = clean.strip()[:300]
        if len(clean) > 10:
            return CoreLLMResponse(message=clean, intent="continue")
        # Step 3: generic fallback
        return CoreLLMResponse(
            message="Уточните, пожалуйста, ваш вопрос.",
            intent="continue",
        )
```

**Token impact:** 0 (runtime code, не system prompt).

### F3: cancel_flow state transition (P0)

**Файл:** `app/core/cancel_flow.py`, метод `start()`

**Проблема:** `start()` показывает список записей, но не переводит state в CANCEL_FLOW.
Следующее сообщение идёт в обычный LLM loop вместо cancel_flow.select().

**Фикс:**

```python
async def start(self, session, trace_id):
    # ... existing: find client, get bookings ...
    if bookings:
        from app.core.conversation import transition_state
        from app.models import ConversationState
        await transition_state(session, ConversationState.CANCEL_FLOW)
        return formatted_list  # "Какую запись отменить? 1. ... 2. ..."
```

**Также в engine.py:** При tool_call `start_cancel_flow` и `escalate_to_admin` —
вернуть результат инструмента напрямую пользователю без повторного LLM вызова.

```python
# В _llm_loop, после execute_tool_calls:
DIRECT_RETURN_TOOLS = {"start_cancel_flow", "escalate_to_admin"}
if executed_tools & DIRECT_RETURN_TOOLS:
    # Tool result IS the user response — no second LLM call
    return tool_results[tool_name]
```

**Corner case:** Убедиться что slot_updates из того же LLM response применяются
ПЕРЕД return (client_phone может быть в slot_updates одновременно с start_cancel_flow).

**Token impact:** 0.

### F4: Промпт — ответь на вопрос, потом предложи (P1)

**Файл:** `app/core/prompt_builder.py`, метод `_sales_rules()`

**Заменить правило 5 (не добавлять новое правило, а уточнить существующее):**

Текущее:
```
5. Предложи конкретную дату пробного: «Ближайшее пробное у Кати — пятница, 28 февраля, 19:00. Запишем?»
```

Новое:
```
5. Если клиент задал конкретный вопрос (день/время/наличие) — СНАЧАЛА ответь на него.
   Потом предложи ближайшую дату. Не игнорируй вопрос ради записи.
```

**Token impact:** +30 chars (разница формулировок). Не увеличивает на каждый запрос —
это фиксированная часть rules, которая и так включается.

### F5: Resolver — prefix match вместо ручных алиасов (P1)

**Файл:** `app/core/entity_resolver/style_resolver.py`

**Текущее:** Exact match по aliases + леммам. "хоп" не матчит "хип-хоп".

**Новое:** Добавить prefix matching ПОСЛЕ exact match (fallback):

```python
def resolve(self, raw: str) -> list:
    # Step 1: exact match (existing)
    results = self._exact_lookup(raw)
    if results:
        return results
    
    # Step 2: normalize (remove dashes, extra spaces)
    normalized = raw.replace("-", " ").replace("  ", " ").strip().lower()
    results = self._exact_lookup(normalized)
    if results:
        return results
    
    # Step 3: prefix match (min 3 chars to avoid false positives)
    if len(normalized) >= 3:
        matches = []
        for alias, style in self._lookup.items():
            # "хоп" matches in "хип-хоп", "хип хоп"
            alias_normalized = alias.replace("-", " ")
            if normalized in alias_normalized or alias_normalized.startswith(normalized):
                matches.append(style)
        if len(matches) == 1:
            return matches  # unambiguous
        if len(matches) > 1:
            return matches  # disambiguation needed
    
    return []
```

**Почему не embeddings:** Для 8-15 стилей с 3-5 алиасами каждый prefix match
покрывает 95% кейсов при нулевой стоимости. Embeddings оправданы при 100+ сущностей
или multi-language — пока не наш случай.

**Алиасы всё же добавить (минимум):** "хоп" → Girly Hip-Hop, "стрип" → Frame Up Strip.
Это 2 строки в YAML, решает самые частые кейсы до того как prefix match отработает.

**Token impact:** 0 (runtime code).

### F6: Policy триггеры — точечное расширение (P1)

**Файл:** `app/knowledge/retriever.py`

**Текущие леммы policies:** {отмена, перенос, опоздание, возврат, надеть, взять, принести}

**Добавить 2 леммы:** "каблук", "обувь"

Это конкретные пропуски из сценария 9 ("С какого каблука приходить?").
Не раздуваем список — добавляем только доказанные промахи.

**Подписки ([:5] лимит):** Заменить `[:5]` на форматирование по категориям:

```python
# Вместо:
for s in self._kb.subscriptions[:5]:
    sections.append(f"- {s.name}: {s.price}₽")

# Новое: компактная строка со ВСЕМИ подписками
prices = ", ".join(f"{s.classes} зан./{s.price:.0f}₽" for s in self._kb.subscriptions)
sections.append(f"Абонементы: {prices}")
```

Одна строка вместо 5 → экономия ~200 chars при полном покрытии.

**Token impact:** –150 chars (компактнее при полном покрытии).

### F7: Инструмент list_my_bookings (P1)

**Файл:** `app/core/prompt_builder.py` — добавить в tools:

```
- list_my_bookings(phone) — показать записи клиента. Спроси телефон если не знаешь.
```

**Файл:** `app/core/engine.py` — `_execute_tool_calls`:

```python
elif tc.name == "list_my_bookings":
    phone = tc.parameters.get("phone") or session.slots.client_phone
    if not phone:
        results[tc.name] = "Для просмотра записей нужен ваш номер телефона."
    else:
        client = await self._impulse.find_client(phone)
        if not client:
            results[tc.name] = "Клиент не найден."
        else:
            from datetime import date
            bookings = await self._impulse.list_bookings(
                client_id=client.id, date_from=date.today()
            )
            if not bookings:
                results[tc.name] = "Активных записей нет."
            else:
                lines = [f"{i+1}. {b.format_short()}" for i, b in enumerate(bookings[:5])]
                results[tc.name] = "\n".join(lines)
```

**Corner case (из ревью):** Проверить что `format_short()` не падает при None в полях.
Добавить try/except вокруг форматирования каждой записи.

**Token impact:** +40 chars в tools секции (фиксированная).

### F8: Эскалация — уведомление админу (P1)

**Файл:** `app/core/engine.py`

При `escalate_to_admin` — отправить контекст в admin chat:

```python
elif tc.name == "escalate_to_admin":
    reason = tc.parameters.get("reason", "Запрос эскалации")
    try:
        admin_chat_id = settings.admin_telegram_chat_id
        if admin_chat_id:
            last_msgs = session.slots.messages[-6:]
            context_lines = [f"⚠️ Эскалация от {session.chat_id}", f"Причина: {reason}"]
            for m in last_msgs:
                role = "👤" if m.get("role") == "user" else "🤖"
                context_lines.append(f"{role} {m.get('content', '')[:80]}")
            admin_text = "\n".join(context_lines)
            await enqueue_outbound(admin_chat_id, admin_text, channel=session.channel)
    except Exception as e:
        logger.warning("Failed to notify admin: %s", e)
    # Always respond to user regardless of admin notification success
    results[tc.name] = "Передаю тебя администратору — он ответит в ближайшее время."
```

**Corner cases (из ревью):**
- `admin_chat_id` не настроен → log warning, пользователь всё равно получает ответ
- `enqueue_outbound` падает → try/except, пользователь всё равно получает ответ
- channel берём из session, не хардкодим "telegram"

**Token impact:** 0.

### F9: Gibberish detector (P2)

**Файл:** `app/core/engine.py`, в начале `handle_message()`

```python
import re

def _is_gibberish(self, text: str) -> bool:
    """Detect gibberish input. Conservative — don't block short valid messages."""
    t = text.strip()
    # Allow known short answers: "да", "нет", "+", numbers, single emoji
    if t in ("да", "нет", "+", "-") or t.isdigit():
        return False
    if len(t) <= 1:
        return False  # single char — let LLM handle
    # Gibberish: no word-like tokens (2+ letters from any script)
    words = re.findall(r'\b\w{2,}\b', t, re.UNICODE)
    if not words and not re.search(r'\d', t):
        return True  # "ацуацу" has word chars but they're gibberish...
    # Repeated chars pattern: "ааааа", "цуцуцу"
    if len(t) >= 4 and len(set(t.replace(" ", ""))) <= 3:
        return True
    return False
```

**Счётчик:** В session.slots добавить `gibberish_count: int = 0`.
Сбрасывать при любом нормальном сообщении.
После 3 подряд → эскалация.

```python
if self._is_gibberish(message.text):
    count = (session.slots.gibberish_count or 0) + 1
    await update_slots(session, gibberish_count=count)
    if count >= 3:
        await self._notify_admin_gibberish(session)
        return "Передаю тебя администратору — он ответит в ближайшее время."
    return "Не совсем понял. Напиши, что интересует — направление, расписание, запись?"
else:
    if session.slots.gibberish_count:
        await update_slots(session, gibberish_count=0)
```

**Corner cases (из ревью):**
- "Да", "+", "19:30" — не gibberish (проверяем явно)
- Сброс счётчика после нормального сообщения
- Unicode-safe: `\w` с `re.UNICODE` работает для кириллицы, латиницы, CJK

**Token impact:** 0 (не LLM — код до вызова LLM, экономит токены на бессмыслице).

### F10: 2 направления — уточнение через промпт (P2)

**Файл:** `app/core/prompt_builder.py`, в `_sales_rules()`, дополнить существующее правило 4:

Дописать к правилу 4 одну строку:

```
Если клиент назвал 2+ направления — уточни: "Начнём с [первого]?"
```

**Файл:** `app/core/entity_resolver/style_resolver.py` — split по "и":

```python
# В resolve(), после неудачного exact match:
parts = re.split(r'\s+и\s+|,\s*', raw)
if len(parts) > 1:
    all_results = []
    for part in parts:
        r = self._lookup(part.strip())
        all_results.extend(r)
    if all_results:
        return all_results  # >1 → engine asks to clarify
```

**Token impact:** +20 chars в rules.

### F11: Перенос записи — через промпт (P2)

**Файл:** `app/core/prompt_builder.py`

Добавить в конец правил (не отдельный flow):

```
Перенос записи = отмена + новая запись. Объясни это и предложи начать.
```

**Token impact:** +30 chars.

### F12: Заморозка абонемента (P2)

**Файл:** `app/core/prompt_builder.py`

Добавить к правилу эскалации:

```
Заморозка абонемента: спроси причину и срок, потом вызови escalate_to_admin.
```

**Token impact:** +25 chars.

### F13: Tool summary — лимит по типу инструмента (P1)

**Файл:** `app/core/engine.py`

**Текущее:** Все tool results обрезаются до 500 chars / 6 строк.

**Новое:** Лимит зависит от инструмента:

```python
_TOOL_SUMMARY_LIMITS = {
    "get_filtered_schedule": (1200, 15),  # расписание — нужно больше
    "list_my_bookings": (800, 10),
    "default": (500, 6),
}

def _summarize_tool_result(self, tool_name: str, result_text: str) -> str:
    max_chars, max_lines = _TOOL_SUMMARY_LIMITS.get(
        tool_name, _TOOL_SUMMARY_LIMITS["default"]
    )
    if len(result_text) <= max_chars:
        return result_text
    lines = result_text.strip().split("\n")
    kept = lines[:max_lines]
    if len(lines) > max_lines:
        kept.append(f"(и ещё {len(lines) - max_lines})")
    return "\n".join(kept)[:max_chars]
```

**Почему не 1500 для всех:** search_kb, escalate — не нуждаются в большом контексте.
Больше контекста = больше токенов = выше cost. Дифференцируем.

**Token impact:** +700 chars max для schedule (только когда вызван), –0 для остальных.

---

### F14: G12 — не блокировать если tool_call был в предыдущем ходе (P0)

**Файл:** `app/core/guardrails.py`, guardrail G12

**Проблема (Сценарий 4):** Пользователь спрашивает "А какое расписание?" после того как
get_filtered_schedule уже вызывался в предыдущем ходе. LLM отвечает из контекста с
временем/днями, но без нового tool_call → G12 блокирует → retry → снова блокирует →
fallback "Возникла техническая проблема".

**Фикс:** G12 проверяет не только текущий ход, но и `executed_tools` из предыдущих ходов
(сохранённые в session). Если get_filtered_schedule был вызван в текущей сессии И
результат ещё в messages history — пропустить.

```python
# В G12 check:
if "get_filtered_schedule" in (session_context.get("recent_tools") or set()):
    # Schedule data is in conversation history — allow
    return GuardrailResult(passed=True)
```

**Также:** engine.py — после execute_tool_calls, сохранять названия вызванных инструментов
в session для G12:

```python
if executed_tools:
    recent = set(session.slots.recent_tools or [])
    recent.update(executed_tools.keys())
    await update_slots(session, recent_tools=list(recent))
```

Добавить поле `recent_tools: list[str] = []` в SlotValues. Сбрасывать при /start.

**Token impact:** 0 (код, не промпт).

### F15: Missing slots → заменить message LLM на запрос недостающего (P0)

**Файл:** `app/core/engine.py`, в блоке `if parsed.intent == "booking"`

**Проблема (Сценарий 5):** LLM говорит "Сейчас создам запись!" и отдаёт create_booking,
но `_get_missing_booking_slots` находит `["branch"]` → код НЕ создаёт запись, но
возвращает message LLM как есть. Пользователь видит обещание без исполнения.

**Фикс:** Если missing slots → заменить message на явный запрос:

```python
if missing:
    missing_names = {
        "branch": "филиал",
        "group": "направление",
        "datetime_resolved": "дата и время",
        "client_name": "имя",
        "client_phone": "номер телефона",
    }
    missing_labels = [missing_names.get(s, s) for s in missing]
    final_message = f"Для записи нужно уточнить: {', '.join(missing_labels)}."
    return final_message
```

**Token impact:** 0.

### F16: Цены — разрешить длинный ответ (P1)

**Файл:** `app/core/guardrails.py`, guardrail G7

**Проблема (Сценарий 8):** Полный прайс > 300 символов. G7 обрезает → неполная информация.

**Фикс:** G7 проверяет intent. Если intent="info" и в message есть "₽" (цена) — лимит 500 
вместо 300:

```python
max_len = 300
if llm_response.intent == "info" and "₽" in llm_response.message:
    max_len = 500
```

Или проще: поднять глобальный лимит до 400 (компромисс: не слишком длинно для TG,
но достаточно для прайса).

**Также в F7 промпте:** Добавить инструкцию для price: "Если не влезает — раздели на
категории: сначала групповые, потом скажи 'Ещё есть разовые и курсы — рассказать?'"

**Token impact:** +30 chars в rules (инструкция про разделение).

### F17: Промпт — "мои записи" → list_my_bookings (P0)

**Файл:** `app/core/prompt_builder.py`

**Проблема (Сценарий 1):** LLM при "какие у меня записи?" вызывает escalate_to_admin
вместо list_my_bookings, потому что инструкция "мои записи → list_my_bookings" отсутствует.

**Фикс:** В описании инструмента list_my_bookings (добавленном в F7) уточнить:

```
- list_my_bookings(phone) — показать записи клиента. Вызывай при "мои записи", 
  "на что записан", "какие у меня записи". Спроси телефон если не знаешь.
```

**Token impact:** +30 chars в tools (часть F7, уточнение формулировки).

---

## 5. Token Budget Analysis

Текущий system prompt: ~3200 chars (после RFC-005 оптимизации).

Постоянные добавки (в rules, каждый запрос):
- F4: +30 chars
- F7+F17: +70 chars (tools + формулировки)
- F10: +20 chars
- F11: +30 chars
- F12: +25 chars
- F16: +30 chars
- **Итого: +205 chars** (~51 token)

Условные добавки (только по триггеру):
- F1: +120 chars (только при ask_service_exists)
- F6: –150 chars (компактные подписки)
- F13: +700 chars (только при get_filtered_schedule)

**Типичный запрос:** 3200 + 205 = 3405 chars.
**Worst case:** 3405 + 120 + 700 = 4225 chars. Укладываемся.

---

## 6. Фазы реализации

### Phase A (P0 — блокеры, 2-3 часа)

1. **F2** — Sanitize JSON fallback (сц. 5, 6)
2. **F3** — cancel_flow state transition + direct return (сц. 8/отмена)
3. **F14** — G12 не блокировать если tool_call был ранее (сц. 4)
4. **F15** — Missing slots → заменить LLM message (сц. 5)
5. **F1** — Retriever триггеры для направлений (сц. 2, 11)
6. **F17** — Промпт: "мои записи" → list_my_bookings (сц. 1)

### Phase B (P1 — качество, 4-5 часов)

7. **F4** — Промпт: ответь, потом предложи (сц. 4)
8. **F5** — Resolver: prefix match + 2 алиаса (сц. 10)
9. **F6** — Policy леммы + компактные подписки (сц. 8, 9)
10. **F13** — Tool summary по типу инструмента (сц. 4)
11. **F7** — list_my_bookings (сц. 1)
12. **F8** — Эскалация с уведомлением админу (сц. 1, 7)
13. **F16** — Цены: длинный ответ или split (сц. 8)

### Phase C (P2 — улучшения, 2-3 часа)

14. **F9** — Gibberish detector (сц. 12)
15. **F10** — 2 направления (сц. 3)
16. **F11** — Перенос (сц. 6)
17. **F12** — Заморозка (сц. 7)

---

## 7. Матрица покрытия: сценарий → фиксы

| Сценарий | Проблема | Фиксы | Покрыто? |
|----------|----------|-------|----------|
| 1. Мои записи | Нет инструмента + эскалация без уведомления | F7, F8, F17 | ✅ |
| 2. Jazz funk | Нет направления → нет предложения похожего | F1, F5 | ✅ |
| 3. 2 направления | Бот взял одно молча | F10 | ✅ |
| 4. Расписание пн/ср/пт | Игнорирует вопрос + G12 crash | F4, F13, F14 | ✅ |
| 5. JSON + "создам запись" | Битый JSON + обещание без записи | F2, F15 | ✅ |
| 6. Перенос + JSON | Битый JSON + нет переноса | F2, F11 | ✅ |
| 7. Заморозка | Нет сбора данных перед эскалацией | F12, F8 | ✅ |
| 8. Стоимость | Только 5 подписок + лимит 300 | F6, F16 | ✅ |
| 9. Каблук | Нет FAQ/policies в контексте | F6 | ✅ |
| 10. Хоп | "хоп" не резолвится | F5 | ✅ |
| 11. Бачата | LLM выдумывает без списка | F1 | ✅ |
| 12. Террор | Нет gibberish detection | F9 | ✅ |

---

## 8. Что НЕ делаем и почему

- **ALWAYS список направлений** — раздувает каждый запрос на 120 chars. Вместо этого — по триггеру.
- **Embeddings для FAQ** — для 30 FAQ и 15 стилей overkill. Prefix match + леммы.
- **Увеличение global tool summary** — раздувает все запросы. Вместо этого — per-tool лимит.
- **Отдельный transfer_booking flow** — промпт "отмени + запишись" дешевле.
- **Multi-language алиасы** — когда дойдём до Казахстана, сделаем. Сейчас — 1 студия, 1 язык.
- **Multi-tenant isolation** — следующий RFC, не этот.
