# Cursor Playbook — RFC-005: Group Availability (Stickers)

## Контекст для Cursor

RFC-005 добавляет проверку доступности групп через стикеры CRM.
Стикеры приходят из `schedule/addition` endpoint (не из schedule/list).
schedule/list возвращает шаблоны (regular=true, day=5 = "каждую пятницу").
schedule/addition возвращает стикеры привязанные к конкретным датам.

**Зависимости:** RFC-003 (engine.py), RFC-004 (entity resolver) уже реализованы.

---

## Phase 5.1: Protocol + Classifier

### Промпт 5.1.1 — AvailabilityStatus + GroupAvailability + Protocol
```
Read RFC_005_GROUP_AVAILABILITY.md sections 5.1 and 5.2.

Create app/core/availability/__init__.py and app/core/availability/protocol.py.

protocol.py must contain:
- AvailabilityStatus enum: OPEN, CLOSED, PRIORITY, INFO, HOLIDAY (5 values)
- GroupAvailability dataclass: schedule_id (int), date (date), status (AvailabilityStatus), sticker_text (str|None), note (str|None)
- GroupAvailabilityProvider Protocol with 3 methods:
  - get_availability(schedule_id: int, target_date: date) -> GroupAvailability
  - find_next_open(schedule_id: int, from_date: date, max_weeks: int = 4) -> GroupAvailability | None
  - find_alternatives(style_id: int, branch_id: int | None, from_date: date, teacher_id: int | None = None) -> list[GroupAvailability]

__init__.py exports: AvailabilityStatus, GroupAvailability, GroupAvailabilityProvider.

File must be under 60 lines. Show plan first.
```

### Промпт 5.1.2 — StickerMapping config + KB validation
```
Read RFC_005_GROUP_AVAILABILITY.md section 8 (studio.yaml config).
Read knowledge/base.py to understand how KB is loaded.

1. Add StickerMapping model to knowledge/base.py (or a separate availability config):
   - open_keywords: list[str]
   - closed_keywords: list[str]
   - priority_keywords: list[str]
   - holiday_keywords: list[str]
   - info_keywords: list[str]
   - unknown_action: Literal["open", "closed"] = "open"

2. Add AvailabilityConfig model:
   - sticker_mapping: StickerMapping
   - max_lookahead_weeks: int = 4

3. Add the availability section to knowledge/studio.yaml:
   availability:
     sticker_mapping:
       open_keywords: ["МОЖНО ПРИСОЕДИНИТЬСЯ"]
       closed_keywords: ["НЕЛЬЗЯ ПРИСОЕДИНИТЬСЯ", "НЕЛЬЗЬЯ ПРИСОЕДИНИТЬСЯ", "ЗАКРЫТО"]
       priority_keywords: ["НОВАЯ ХОРЕОГРАФИЯ"]
       holiday_keywords: ["ВЫХОДНОЙ"]
       info_keywords: ["СТАРТ", "ОТКРЫТЫЙ УРОК"]
       unknown_action: "open"
     max_lookahead_weeks: 4

4. KB loader must parse availability config. If section missing → use defaults (all lists empty, unknown_action="open").

Do NOT modify engine.py, prompt_builder.py, or any existing resolver files.
Show plan first, then code.
```

### Промпт 5.1.3 — Sticker classifier
```
Read RFC_005_GROUP_AVAILABILITY.md section 5.3.2 (classification) and 5.3.3 (multiple stickers).

Create app/core/availability/classifier.py.

Must contain:
1. classify_sticker(name: str, config: StickerMapping) -> AvailabilityStatus
   - name_upper = name.strip().upper()
   - Check order: CLOSED first (safety-first), then HOLIDAY, OPEN, PRIORITY, INFO
   - Each check: if keyword.upper() IN name_upper (substring match, not exact)
   - Unknown → log warning "unknown_sticker" with name, return OPEN if unknown_action="open"
   - Use structlog logger

2. resolve_multiple_stickers(schedule_id: int, target_date: date, stickers: list[dict], config: StickerMapping) -> GroupAvailability
   - Classify each sticker
   - Priority: CLOSED/HOLIDAY beats all, then PRIORITY, then OPEN
   - If all INFO → return OPEN with note="info_only"
   - sticker_text = the winning sticker's name

File must be under 70 lines. Pure functions, no side effects, no I/O.
Do NOT use LLM for classification. This is deterministic.
Show plan first, then code.
```

### Промпт 5.1.4 — Classifier unit tests
```
Read app/core/availability/classifier.py.

Create tests/unit/test_sticker_classifier.py with these test cases:

1. test_exact_open — "МОЖНО ПРИСОЕДИНИТЬСЯ" → OPEN
2. test_exact_closed — "НЕЛЬЗЯ ПРИСОЕДИНИТЬСЯ" → CLOSED
3. test_typo_closed — "НЕЛЬЗЬЯ ПРИСОЕДИНИТЬСЯ" → CLOSED (real typo from CRM)
4. test_substring_closed — "НАБОР ЗАКРЫТ" → CLOSED (contains "ЗАКРЫТ")
5. test_new_choreo — "НОВАЯ ХОРЕОГРАФИЯ" → PRIORITY
6. test_holiday — "ВЫХОДНОЙ" → HOLIDAY
7. test_info_start — "СТАРТ АКВАКУРСА" → INFO (partial match on "СТАРТ")
8. test_unknown_default_open — "КАКОЙ-ТО НОВЫЙ ТЕКСТ" → OPEN (unknown_action="open")
9. test_unknown_action_closed — same text but unknown_action="closed" → CLOSED
10. test_case_insensitive — "можно присоединиться" (lowercase) → OPEN
11. test_closed_beats_open — two stickers ["МОЖНО ПРИСОЕДИНИТЬСЯ", "НЕЛЬЗЯ ПРИСОЕДИНИТЬСЯ"] → CLOSED
12. test_priority_when_no_closed — ["НОВАЯ ХОРЕОГРАФИЯ", "МОЖНО ПРИСОЕДИНИТЬСЯ"] → PRIORITY
13. test_all_info_returns_open — [{"name": "СТАРТ АКВАКУРСА"}] → OPEN with note="info_only"
14. test_empty_stickers — [] → should not be called (but handle gracefully if it is)

Use a fixture for default StickerMapping config matching studio.yaml.
Run: python -m pytest tests/unit/test_sticker_classifier.py -v
```

---

## Phase 5.2: Schedule Expander

### Промпт 5.2.1 — ScheduleExpander
```
Read RFC_005_GROUP_AVAILABILITY.md section 6.
Read app/integrations/impulse/models.py to understand Schedule model fields:
  - regular: bool | None
  - day: int | None (0=Mon, 6=Sun)
  - minutes_begin: int | None
  - minutes_end: int | None
  - date_begin: int | None (Unix timestamp for one-time classes)

Create app/core/availability/schedule_expander.py.

Must contain:
1. ExpandedSlot dataclass:
   - schedule_id: int
   - date: date
   - time_begin: time
   - time_end: time
   - group_name: str
   - teacher_name: str | None
   - branch_name: str | None
   - style_id: int | None
   - branch_id: int | None
   - teacher_id: int | None
   - availability: AvailabilityStatus = AvailabilityStatus.OPEN

2. expand_schedule(templates: list[Schedule], from_date: date, to_date: date) -> list[ExpandedSlot]
   - For regular=True: find all dates with matching weekday in [from_date, to_date]
   - For regular=False with date_begin: check if date falls in range
   - Skip templates where regular=None and date_begin=None
   - Convert minutes_begin/minutes_end to time objects
   - Sort result by (date, time_begin)
   - Impulse day 0=Mon matches Python weekday() 0=Mon

3. Helper: _minutes_to_time(minutes: int | None) -> time

File under 80 lines. No I/O, no async, pure computation.
Timezone: NOT needed here — we work with dates only, timestamps converted at edges.
Show plan first, then code.
```

### Промпт 5.2.2 — ScheduleExpander unit tests
```
Read app/core/availability/schedule_expander.py.
Read app/integrations/impulse/models.py for Schedule constructor.

Create tests/unit/test_schedule_expander.py:

1. test_regular_weekly — Schedule(regular=True, day=5, minutesBegin=1170) with from=Mon to Sun
   → expect 1 slot on Friday (Impulse day=5=Пт), time_begin=19:30
2. test_regular_two_weeks — same schedule, 14-day range → 2 slots
3. test_one_time_in_range — Schedule(regular=False, dateBegin=unix_ts_for_march_5) → 1 slot
4. test_one_time_out_of_range — dateBegin outside range → 0 slots
5. test_mixed — 1 regular + 1 one-time → correct number of slots, sorted by date
6. test_sort_order — multiple templates on same date → sorted by time_begin
7. test_no_templates — empty list → empty result
8. test_null_fields — Schedule with regular=None, date_begin=None → skip gracefully

Use real Schedule model from impulse/models.py with minimal required fields.
Mock only the nested objects (group, teacher, branch) with simple dicts or None.
Run: python -m pytest tests/unit/test_schedule_expander.py -v
```

---

## Phase 5.3: Impulse Sticker Provider

### Промпт 5.3.1 — adapter.get_additions()
```
Read RFC_005_GROUP_AVAILABILITY.md section 4.2.
Read app/integrations/impulse/adapter.py and app/integrations/impulse/client.py.

Add method to ImpulseAdapter:

async def get_additions(self, target_date: date) -> list[dict]:
    """Fetch sticker additions for a specific date.
    
    Calls POST /api/public/schedule/addition with date filter.
    Caches result in Redis: impulse:cache:schedule:additions:{YYYY-MM-DD}, TTL 15min.
    
    Returns raw items list from API response.
    """

Implementation notes:
- The endpoint path is "public/schedule" with action "addition"
  → self.client._request("POST", "public/schedule", "addition", body)
  → this will construct URL: base_url/api/public/schedule/addition
  IMPORTANT: check that client._request can handle "public/schedule" as entity.
  If client constructs URL as f"{base_url}/api/{entity}/{action}",
  then entity="public/schedule" will work: base_url/api/public/schedule/addition.
  If not → may need to adjust. Show plan first.

- Body: {"date": unix_timestamp}
  Convert date to unix timestamp: start of day in Asia/Vladivostok timezone.
  from zoneinfo import ZoneInfo
  from datetime import datetime, time as dt_time
  ts = int(datetime.combine(target_date, dt_time.min, tzinfo=ZoneInfo("Asia/Vladivostok")).timestamp())

- Cache: use existing self.cache with entity="schedule", key_parts=("additions", target_date.isoformat())
  TTL: same as schedule (15 min)

- Error handling: same pattern as get_schedule — try/except, error_handler, fallback queue.

- Response format: {"total": N, "items": [...]}
  Return data.get("items", [])

Do NOT modify client.py URL construction. Only add get_additions to adapter.py.
Show plan first, then code.
```

### Промпт 5.3.2 — ImpulseStickerProvider
```
Read RFC_005_GROUP_AVAILABILITY.md sections 5.3.1, 5.3.2, 5.3.3.
Read app/core/availability/protocol.py and app/core/availability/classifier.py.
Read app/integrations/impulse/adapter.py (new get_additions method).

Create app/core/availability/impulse_provider.py.

class ImpulseStickerProvider:
    """Implements GroupAvailabilityProvider for Impulse CRM stickers."""

    def __init__(self, adapter: ImpulseAdapter, config: StickerMapping):
        self._adapter = adapter
        self._config = config

    async def get_availability(self, schedule_id: int, target_date: date) -> GroupAvailability:
        - Call self._adapter.get_additions(target_date)
        - Filter items where item["schedule"]["id"] == schedule_id
        - If no matching stickers → return GroupAvailability(status=OPEN)
        - If stickers found → call resolve_multiple_stickers from classifier.py
        - Wrap errors gracefully → return OPEN on failure (don't block booking on sticker fetch error)

    async def find_next_open(self, schedule_id: int, from_date: date, max_weeks: int = 4) -> GroupAvailability | None:
        - For each week in range(max_weeks):
            - Calculate next occurrence date for this schedule's day of week
            - Call get_availability(schedule_id, that_date)
            - If status is OPEN or PRIORITY → return it
        - Need to know the weekday of this schedule → accept it as parameter or fetch from adapter
        - SIMPLE APPROACH: accept day_of_week: int parameter (caller knows it from Schedule template)
        - Return None if nothing found in max_weeks

    async def find_alternatives(self, style_id: int, branch_id: int | None, from_date: date, teacher_id: int | None = None) -> list[GroupAvailability]:
        - Get all schedule templates from adapter.get_schedule()
        - Filter by style_id (via group.style_id)
        - Expand to next 2 weeks using expand_schedule
        - For each expanded slot: get_availability → keep only OPEN/PRIORITY
        - Sort by priority: same branch first, then same teacher, then other
        - Limit to 5 results

File under 130 lines. 
Use structlog for logging.
Do NOT modify engine.py or prompt_builder.py yet.
Show plan first, then code.
```

### Промпт 5.3.3 — Provider unit tests
```
Read app/core/availability/impulse_provider.py.

Create tests/unit/test_availability_provider.py.

Mock ImpulseAdapter.get_additions to return controlled data.
Mock ImpulseAdapter.get_schedule to return controlled templates.

Test cases:

1. test_get_availability_no_stickers — get_additions returns [] → OPEN
2. test_get_availability_closed — returns sticker with name "НЕЛЬЗЯ ПРИСОЕДИНИТЬСЯ" → CLOSED
3. test_get_availability_open — returns sticker with name "МОЖНО ПРИСОЕДИНИТЬСЯ" → OPEN
4. test_get_availability_filters_by_schedule_id — multiple stickers, only matches target schedule_id
5. test_get_availability_error_returns_open — get_additions raises exception → returns OPEN (graceful degradation)

6. test_find_next_open_immediate — first date is OPEN → returns it
7. test_find_next_open_skip_closed — first 2 dates CLOSED, third OPEN → returns third
8. test_find_next_open_none_found — all dates CLOSED for max_weeks → None

9. test_find_alternatives_same_branch_first — 2 alternatives, one same branch, one different → same branch first
10. test_find_alternatives_no_open — all alternatives closed → empty list

Use pytest fixtures for StickerMapping config and mock adapter.
Use unittest.mock.AsyncMock for async methods.
Run: python -m pytest tests/unit/test_availability_provider.py -v
```

---

## Phase 5.4: Engine Integration

### Промпт 5.4.1 — Schedule format with availability markers
```
Read RFC_005_GROUP_AVAILABILITY.md section 7.2.
Read app/core/prompt_builder.py — find where schedule data is formatted for LLM.
Read app/core/engine.py — find where generate_schedule_response is called.

Goal: When the bot shows schedule to the user, each slot must have an availability marker.

Approach: In the schedule formatting step (either prompt_builder or engine's schedule flow),
after getting schedule data, also fetch sticker additions and mark each slot.

1. In engine.py, in the schedule display flow:
   - After expanding schedule to concrete dates
   - For each unique date in the expanded schedule: call availability_provider.get_additions(date)
   - For each slot: call availability_provider.get_availability(schedule_id, slot_date)
   - Add status marker to the formatted schedule string:
     OPEN → "✅"
     CLOSED → "❌ ЗАКРЫТО"
     PRIORITY → "⭐ НОВАЯ ХОРЕОГРАФИЯ"
     HOLIDAY → "🚫 ВЫХОДНОЙ"
     INFO → "" (no marker)

2. In prompt_builder.py, add to _constraints (or equivalent):
   - "Не предлагай клиенту даты помеченные ❌ ЗАКРЫТО или 🚫 ВЫХОДНОЙ."
   - "Предпочитай даты помеченные ⭐ НОВАЯ ХОРЕОГРАФИЯ — это лучший момент для записи новичков."

IMPORTANT: If availability_provider is not initialized (None) or fetch fails → show schedule WITHOUT markers. Don't break existing flow.

Do NOT modify classifier.py, protocol.py, or impulse_provider.py.
Show plan first, then code.
```

### Промпт 5.4.2 — Booking availability check + _handle_closed_group
```
Read RFC_005_GROUP_AVAILABILITY.md section 7.1 and 7.3.
Read app/core/engine.py — find where booking is confirmed (slots.confirmed, create_booking call).

Add pre-booking availability check:

1. In engine.py, before create_booking is executed:
   - If availability_provider is available:
     avail = await self._availability.get_availability(schedule_id, target_date)
     if avail.status in (CLOSED, HOLIDAY):
         return await self._handle_closed_group(session, slots, avail)

2. Implement _handle_closed_group(self, session, slots, avail) -> str:
   Step 1: find_next_open(schedule_id, from_date=target_date, day_of_week=slots.day_of_week)
   - If found: "На {date} запись в эту группу закрыта. Ближайшая открытая дата — {next_date}. Записать?"
     - Clear slots.confirmed, set slots.target_date to next_date
     - Return message

   Step 2: If not found → find_alternatives(style_id, branch_id, from_date)
   - If found: "Все ближайшие даты в этой группе закрыты. Есть {alt_group} в {alt_branch}, {alt_date} в {alt_time}. Подойдёт?"
     - Clear booking slots, set new values from alternative
     - Return message

   Step 3: If no alternatives → escalation offer:
   - "Сейчас все группы по {style} закрыты для записи. Передать администратору, чтобы добавили тебя в лист ожидания?"
     - Set session state to wait for confirmation
     - On "да" → escalate_to_admin(reason="Лист ожидания: {phone}, {style}, {branch}")
     - Return message

3. _handle_closed_group must be graceful: if any step fails → fall through to next.
   If everything fails → "Уточню у администратора и напишу тебе. Подожди немного!" + escalate.

Do NOT change the booking confirmation flow for OPEN groups.
Do NOT modify classifier.py, prompt_builder.py, or availability provider.
Show plan first, then code.
```

### Промпт 5.4.3 — Guardrail G14
```
Read RFC_005_GROUP_AVAILABILITY.md section 7.4.
Read app/core/guardrails.py — understand existing guardrail pattern.

Add G14 — block booking for closed groups:

1. New method _g14_closed_group_booking(self, llm_response, availability_cache: dict | None) -> list[str]:
   - Check if any tool_call has name "create_booking"
   - If yes, extract schedule_id and target_date from parameters
   - Look up in availability_cache (dict of f"{schedule_id}:{date}" → GroupAvailability)
   - If status is CLOSED or HOLIDAY → return ["G14: booking blocked — group closed ({sticker_text})"]
   - Otherwise return []

2. Add availability_cache parameter to check() method:
   async def check(self, ..., availability_cache: dict | None = None) -> GuardrailResult

3. Call _g14_closed_group_booking in the guardrails check sequence.

4. In engine.py, when calling guardrails, pass availability_cache if provider is active.
   The cache is populated during the schedule display step.

Keep it simple: G14 is a hard block. If it fires → response is blocked, 
engine returns the _handle_closed_group message instead.

Do NOT modify G1-G13. Do NOT modify classifier.py or provider.
Show plan first, then code.
```

### Промпт 5.4.4 — Startup wiring in main.py
```
Read app/main.py — see how entity resolver is initialized at startup.
Read app/core/availability/impulse_provider.py.

Wire up ImpulseStickerProvider at startup:

1. In main.py lifespan:
   - After impulse adapter is created
   - After KB is loaded (need StickerMapping from KB config)
   - Create ImpulseStickerProvider(adapter=impulse_adapter, config=kb.availability.sticker_mapping)
   - Set it on engine: engine.set_availability_provider(provider)

2. In engine.py:
   - Add _availability: GroupAvailabilityProvider | None = None to constructor
   - Add set_availability_provider(provider) / get_availability_provider() global functions
   - Pattern: same as set_entity_resolver / get_entity_resolver

3. If KB has no availability config → don't create provider, engine works without it (existing behavior).

4. If provider creation fails → log warning, continue without it. Bot must not crash.

Do NOT modify any other files.
Show plan first, then code.
```

---

## Phase 5.5: Integration Tests

### Промпт 5.5.1 — Prompt regression tests for availability
```
Read tests/prompt_regression/ to understand existing test format.
Read RFC_005_GROUP_AVAILABILITY.md section 7.

Create tests/prompt_regression/test_availability.yaml with scenarios:

1. booking_closed_group:
   user: "Запиши меня на стрип к Насте на пятницу"
   context: Schedule for Friday has CLOSED sticker
   expect: response mentions "закрыта" AND offers alternative date or group
   must_not_contain: ["записала", "подтверждаю"]

2. booking_open_with_priority:
   user: "Хочу на хилс в среду"
   context: Wednesday has PRIORITY sticker ("НОВАЯ ХОРЕОГРАФИЯ")
   expect: proceeds with booking normally (PRIORITY = open)

3. schedule_shows_markers:
   user: "Какое расписание на эту неделю?"
   context: Mix of OPEN, CLOSED, PRIORITY stickers
   expect: response includes availability markers (✅, ❌, ⭐)

4. closed_with_alternative_offered:
   user: "Запиши на фрейм ап стрип в пятницу"
   context: Friday CLOSED, but next Friday is OPEN
   expect: offers next Friday as alternative

5. all_closed_escalation:
   user: "Хочу на стрип-пластику"
   context: All dates for this style CLOSED for 4 weeks
   expect: offers to contact admin / waitlist

Show plan first, then code.
```

### Промпт 5.5.2 — End-to-end smoke test
```
Read app/core/availability/ (all files).
Read app/core/engine.py (availability integration).

Create tests/unit/test_availability_integration.py:

Test the full flow with mocked CRM:

1. test_booking_blocked_for_closed_group:
   - Mock adapter to return schedule template + CLOSED sticker
   - Send booking message through engine
   - Assert: no create_booking called, response offers alternative

2. test_booking_proceeds_for_open_group:
   - Mock adapter to return schedule template + no stickers
   - Send booking message through engine
   - Assert: create_booking is called normally

3. test_schedule_display_includes_markers:
   - Mock adapter with mixed stickers
   - Request schedule display
   - Assert: response contains ✅ and ❌ markers

4. test_graceful_degradation_no_provider:
   - Engine without availability provider set
   - Booking proceeds normally (no availability check)

Use AsyncMock for all CRM calls. Test the integration between components,
not individual units.

Run: python -m pytest tests/unit/test_availability_integration.py -v
```

---

## Порядок выполнения

```
5.1.1 → 5.1.2 → 5.1.3 → 5.1.4  (Protocol + Classifier + Config + Tests)
5.2.1 → 5.2.2                    (ScheduleExpander + Tests)
5.3.1 → 5.3.2 → 5.3.3           (Impulse Provider + Tests)
5.4.1 → 5.4.2 → 5.4.3 → 5.4.4  (Engine integration)
5.5.1 → 5.5.2                    (Integration tests)
```

Каждый промпт — один файл или минимальный набор изменений.
Проверяй тесты после каждого phase.

## Блокеры (сделать ДО начала)

- [ ] Проверить формат body schedule/addition (curl)
- [ ] Проверить путь: /api/public/schedule/addition или /api/schedule/addition
- [ ] Согласовать стандартные тексты стикеров с Татьяной
