# Cursor Prompt — RFC-006 Phase 2: Schedule filter by IDs + Guardrails adaptation

## Контекст

Phase 1 завершена:
- KBRetriever создан и интегрирован в prompt_builder
- schedule_data=None в build_system_prompt (расписание убрано из system prompt)
- _CONFIRM_YES расширен
- _summarize_tool_result добавлен
- crm_schedule NameError исправлен на None

Осталось:
- schedule_flow фильтрует по строковым именам → пустой результат при KB≠CRM names
- guardrails G1/G10 раньше проверяли schedule из system prompt → сейчас получают None

## Задача 2.1: Schedule filter по IDs

Файл: `app/core/schedule_flow.py`

Найди функцию `generate_schedule_response`. Сейчас она фильтрует расписание
по строковым именам (slots["group"], slots["teacher"], slots["branch"]).
Это ломается когда KB name ≠ CRM name (напр. "High Heels" vs "Хай хиллс").

### Что изменить

В `generate_schedule_response` (или в функции которая фильтрует schedule entries):

1. Прочитай IDs из slots:
```python
style_id = slots.get("style_id")
branch_id = slots.get("branch_id")  
teacher_id = slots.get("teacher_id")
```

2. Фильтруй по IDs когда они есть, fallback на строки когда нет:
```python
def _matches_filter(entry, style_id, branch_id, teacher_id, group_name, branch_name, teacher_name):
    """Filter schedule entry. IDs take priority over string names."""
    
    # Style filter
    if style_id is not None:
        entry_style_id = None
        if hasattr(entry, 'style_id'):
            entry_style_id = entry.style_id
        elif hasattr(entry, 'group') and isinstance(entry.group, dict):
            style = entry.group.get('style', {})
            if isinstance(style, dict):
                entry_style_id = style.get('id')
        if entry_style_id is not None and str(entry_style_id) != str(style_id):
            return False
    elif group_name:
        # Fallback: string match (less reliable)
        entry_name = getattr(entry, 'style_name', '') or ''
        if group_name.lower() not in entry_name.lower():
            return False
    
    # Branch filter
    if branch_id is not None:
        entry_branch_id = None
        if hasattr(entry, 'branch') and isinstance(entry.branch, dict):
            entry_branch_id = entry.branch.get('id')
        elif hasattr(entry, 'branch_id'):
            entry_branch_id = entry.branch_id
        if entry_branch_id is not None and str(entry_branch_id) != str(branch_id):
            return False
    elif branch_name:
        entry_branch = getattr(entry, 'branch_name', '') or ''
        if branch_name.lower() not in entry_branch.lower():
            return False
    
    # Teacher filter  
    if teacher_id is not None:
        entry_teacher_id = None
        if hasattr(entry, 'teacher_id'):
            entry_teacher_id = entry.teacher_id
        elif hasattr(entry, 'group') and isinstance(entry.group, dict):
            t1 = entry.group.get('teacher1', {})
            if isinstance(t1, dict):
                entry_teacher_id = t1.get('id')
        if entry_teacher_id is not None and str(entry_teacher_id) != str(teacher_id):
            return False
    elif teacher_name:
        entry_teacher = getattr(entry, 'teacher_name', '') or ''
        if teacher_name.lower() not in entry_teacher.lower():
            return False
    
    return True
```

3. Добавь debug print:
```python
print(f"SCHEDULE_FILTER: total={len(schedules)} style_id={style_id} branch_id={branch_id} teacher_id={teacher_id} → {len(filtered)} matches")
```

4. Убедись что `_format_schedule_with_availability` НЕ пере-фильтрует по строкам.
Если там есть `group_filter` string comparison — добавь параметр `pre_filtered=True`
и пропускай string filter когда IDs уже применены.

### Что НЕ менять
- Не меняй adapter.py
- Не меняй schedule_expander.py
- Не меняй сигнатуру generate_schedule_response (если возможно)

---

## Задача 2.2: Guardrails — адаптация G1/G10

Файл: `app/core/guardrails.py`

Сейчас G1 и G10 проверяют schedule data из crm_schedule параметра.
После RFC-006 Phase 1 этот параметр = None.

### Что изменить

1. В сигнатуру `check()` уже передаётся `executed_tools: set[str]`.
   Используй это:

```python
async def check(
    self,
    llm_response,
    slots,
    phase,
    crm_schedule,        # Now always None after RFC-006
    executed_tools=None,  # Already exists
    availability_cache=None,
) -> GuardrailResult:
```

2. **G1 (schedule times in response):**
   - Если executed_tools содержит "get_filtered_schedule" → schedule данные
     были получены через tool call → LLM видел реальные данные → SKIP G1
   - Если НЕ содержит и ответ упоминает время/даты → G12 уже ловит это
   - Поэтому G1 можно упростить: если crm_schedule is None AND 
     "get_filtered_schedule" in executed_tools → pass

```python
# G1: Schedule times verification
if self._mentions_schedule_times(response.message):
    if crm_schedule:
        # Old path: verify against crm_schedule
        if not self._times_in_schedule(response.message, crm_schedule):
            violations.append("G1: времена не найдены в расписании CRM")
    elif "get_filtered_schedule" not in (executed_tools or set()):
        # No schedule data at all and no tool call — G12 handles this
        pass
    # else: tool was called, LLM saw real data — trust it
```

3. **G10 (schedule_id exists):**
   - Если tool call create_booking с schedule_id → проверить что schedule_id
     был в tool results (не в crm_schedule)
   - Но create_booking уже перехватывается в engine.py (converted to intent="booking")
   - G10 реально нужен только если booking идёт напрямую
   - Для безопасности: если crm_schedule is None → skip G10 (idempotency guard
     и CRM API сами отклонят невалидный schedule_id)

```python
# G10: schedule_id verification
if crm_schedule is None:
    pass  # CRM will reject invalid schedule_id via idempotency/API
```

4. **G12 (schedule/price without tool call):**
   Уже работает корректно — проверяет executed_tools.

### Тест

```python
# test_guardrails_no_schedule.py
async def test_g1_passes_when_tool_called():
    """G1 should pass when get_filtered_schedule was executed (RFC-006)."""
    result = await guardrails.check(
        response_with_times,
        slots,
        phase,
        crm_schedule=None,
        executed_tools={"get_filtered_schedule"},
    )
    assert "G1" not in str(result.violations)

async def test_g10_passes_when_no_crm_schedule():
    """G10 should pass when crm_schedule is None (RFC-006)."""
    result = await guardrails.check(
        response_with_booking,
        slots,
        phase,
        crm_schedule=None,
        executed_tools={"get_filtered_schedule"},
    )
    assert "G10" not in str(result.violations)
```

---

## Задача 2.3: Удалить мёртвый код из prompt_builder.py

Файл: `app/core/prompt_builder.py`

Следующие методы больше не вызываются в `build_system_prompt()`:
- `_format_conversation_history`
- `_format_schedule`
- `_format_kb_context`
- `_prices_summary`

НЕ удаляй их сейчас. Вместо этого:
1. Добавь комментарий `# DEPRECATED by RFC-006 — kept for rollback`
2. Убедись что они НЕ вызываются нигде кроме тестов

```bash
grep -rn "_format_conversation_history\|_format_schedule\|_format_kb_context\|_prices_summary" app/ --include="*.py" | grep -v "test_" | grep -v "#"
```

Если находятся вызовы кроме prompt_builder.py → убери.

---

## Verification Phase 2

1. Полный flow: /start → "Хочу к Тане на хиллс" → "Гоголя" → schedule показывается
2. Логи: `SCHEDULE_FILTER: total=211 style_id=22 branch_id=1 teacher_id=15 → N matches` (N > 0)
3. Логи: `SYSTEM_PROMPT_SIZE: <6000`
4. Нет violations G1/G10 при нормальном flow
5. `python -m pytest tests/ -v` — нет регрессий
6. Prompt regression: `python -m pytest tests/prompt_regression/ -v`

---

# Cursor Prompt — RFC-006 Phase 3: Full verification + cleanup

## Контекст

Phase 1 + Phase 2 завершены. Нужно проверить всё вместе и почистить.

## Задача 3.1: End-to-end verification

Запусти бота и проверь эти сценарии вручную. После каждого — проверь логи.

### Сценарий A: Полная запись
```
/start
→ "Хочу на хай хиллс к Тане"
→ [бот спрашивает филиал]
→ "Гоголя"  
→ [бот показывает расписание — НЕ пустое]
→ "Давай на пятницу"
→ [бот спрашивает ФИО и телефон]
→ "Анна Иванова +79001234567"
→ [бот показывает сводку]
→ "Давай"
→ [бот подтверждает запись + адрес + dress code]
```

Проверь в логах:
- `SYSTEM_PROMPT_SIZE: <6000` на каждом шаге
- `SCHEDULE_FILTER: ... → N matches` где N > 0
- `BUILD_MESSAGES: ... ~<15000 chars` (не 262k)
- Нет `LLM 400` ошибок
- "Давай" → confirmation fast path (нет LLM call)

### Сценарий B: FAQ
```
/start
→ "Как отменить занятие?"
→ [бот отвечает из FAQ, НЕ выдумывает]
```

Проверь: `SYSTEM_PROMPT_SIZE: <6000`, ответ содержит "за 2 часа" или аналогичное из KB.

### Сценарий C: Абонемент
```
/start
→ "Сколько стоит абонемент?"
→ [бот показывает цены из KB]
```

Проверь: цены совпадают с studio.yaml, не выдуманы.

### Сценарий D: Edge case — "Давай" без контекста
```
/start
→ "Давай"
→ [бот НЕ падает, спрашивает что интересует]
```

## Задача 3.2: Метрики в БД

```sql
-- Средний prompt_tokens за последние 100 вызовов
SELECT AVG(prompt_tokens) as avg_tokens, 
       MAX(prompt_tokens) as max_tokens,
       MIN(prompt_tokens) as min_tokens
FROM llm_calls 
ORDER BY created_at DESC 
LIMIT 100;
```

Ожидаемые значения:
- avg_tokens < 5000
- max_tokens < 10000
- Нет строк с prompt_tokens > 50000

## Задача 3.3: Prompt regression

```bash
python -m pytest tests/prompt_regression/ -v
```

Порог: ≥ 90% pass. Если < 90% → логи какие тесты фейлят и почему.

## Задача 3.4: Cleanup (после успешной верификации)

Только после того как все сценарии пройдены:

1. Удали debug prints (оставь только SYSTEM_PROMPT_SIZE и SCHEDULE_FILTER):
```bash
grep -rn "print(f\"DEBUG" app/core/engine.py
```

2. В prompt_builder.py — удали deprecated методы:
- `_format_conversation_history`
- `_format_schedule`  
- `_format_kb_context`
- `_prices_summary`

И обнови тесты которые их вызывают.

3. Обнови cursorrules:
```
# В секции Architecture:
- System prompt uses KBRetriever (RFC-006) for compact KB context (~800 tokens)
- Schedule data is NOT in system prompt — only via tool call get_filtered_schedule
- Schedule filtering uses CRM IDs (style_id, branch_id, teacher_id), not string names
```

## DO NOT
- Не удаляй deprecated методы ДО верификации
- Не меняй retriever.py (Phase 1 код стабилен)
- Не меняй adapter.py или client.py
- Не добавляй новых зависимостей
