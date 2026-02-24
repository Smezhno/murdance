# Cursor Playbook — RFC-003: Conversation Engine v2

> **Этот playbook заменяет Phase 3 в CURSOR_PLAYBOOK.md.**
> Phases 1-2 завершены. RFC-002 (миграция на PostgreSQL) реализован.
> Теперь: замена FSM на LLM-driven engine.

## Золотые правила (напоминание)

1. **Один модуль за раз.** Cursor галлюцинирует на третьем файле.
2. **Сначала план, потом код.** "Propose structure, don't write code yet."
3. **Явные ссылки на RFC-003.** Не "сделай engine", а "implement per RFC-003 §7.1".
4. **Проверяй каждый модуль** — unit-тесты и prompt regression до следующего шага.
5. **Не ломай то что работает.** cancel_flow.py, schedule_flow.py, idempotency.py — не трогай.

---

## Pre-flight: обновление cursorrules

> Прежде чем писать код — обнови правила для Cursor, чтобы он не ссылался на удалённые модули.

### Промпт 0.1 — Обновить cursorrules
```
Read RFC_003_CONVERSATION_ENGINE_V2.md sections 2 and 3.

Update the `cursorrules` file with these changes:

1. Replace "Redis (sessions, locks, cache, queues, budgets)" with 
   "PostgreSQL (all data — sessions, cache, queues, budgets, logs). Redis removed per RFC-002."

2. Replace "Docker Compose (app + worker + redis + postgres + caddy)" with
   "Docker Compose (app + worker + postgres + caddy). 4 containers."

3. Replace "Communication: app pushes to Redis list → worker consumes" with
   "Communication: app inserts into outbound_queue (PostgreSQL) → NOTIFY → worker polls with SKIP LOCKED"

4. Replace "FSM: deterministic state machine with slot filling. See CONTRACT §7." with
   "Conversation Engine: LLM-driven slot tracker with guardrails. See RFC-003. Phases computed from slots, not stored as FSM state."

5. Replace "Idempotency: sha256(phone + schedule_id), Redis SETNX" with
   "Idempotency: sha256(phone + schedule_id), PostgreSQL UNIQUE constraint. See core/idempotency.py."

6. In Project Structure, update core/ to:
   ```
   core/
   ├── engine.py           # ConversationEngine — main orchestrator (RFC-003)
   ├── slot_tracker.py     # ConversationPhase, compute_phase()
   ├── guardrails.py       # 12 guardrail checks on LLM output
   ├── prompt_builder.py   # System prompt assembly with slots + KB + tools
   ├── conversation.py     # Session CRUD (PostgreSQL)
   ├── schedule_flow.py    # Schedule fetch + format (deterministic, no LLM)
   ├── cancel_flow.py      # Cancel booking flow
   ├── booking_confirm.py  # Booking creation + receipt
   ├── response_generator.py # LLM response templates
   ├── temporal.py         # Date parser (code, not LLM)
   ├── idempotency.py      # Booking dedup (PostgreSQL)
   └── escalation.py       # Human handoff relay
   ```

7. Remove these from Project Structure (they will be deleted):
   - core/fsm.py
   - core/booking_flow.py  
   - core/slot_collector.py
   - core/intent.py
   - storage/redis.py

Do not write any other code. Only update cursorrules.
```

---

## Phase 3.1: Фундамент

### Промпт 3.1.1 — SlotValues extension + ConversationPhase
```
Read RFC_003_CONVERSATION_ENGINE_V2.md section 4 ("Модель данных").

Task: Extend the existing SlotValues model and create a new slot_tracker module.

Step 1 — PLAN ONLY (no code yet):
- Show which fields you will add to SlotValues in app/models.py
- Show the ConversationPhase enum and compute_phase() function signature
- Confirm that all new fields have default values (backward compat with existing PG sessions)
- Confirm you will NOT modify: cancel_bookings, selected_reservation_id, messages fields

Step 2 — After plan approval, implement:

File 1: app/models.py — Add new fields to SlotValues:
  - branch: str | None = None          (филиал)
  - experience: str | None = None      (уровень: "новичок" | "продолжающий")
  - schedule_shown: bool = False
  - summary_shown: bool = False
  - confirmed: bool = False
  - booking_created: bool = False
  - receipt_sent: bool = False
  ALL new fields MUST have defaults so existing PostgreSQL JSONB deserializes correctly.

File 2: app/core/slot_tracker.py — NEW file, under 80 lines:
  - ConversationPhase enum (9 values per RFC-003 §4.2)
  - compute_phase(slots: SlotValues, is_cancel: bool, is_admin: bool) → ConversationPhase
  - Logic per RFC-003 §4.2 table:
    * No slots → GREETING
    * branch OR group OR experience → DISCOVERY
    * branch AND group → SCHEDULE
    * datetime_resolved AND schedule_shown → COLLECTING_CONTACT
    * client_name AND client_phone AND datetime_resolved → CONFIRMATION
    * confirmed → BOOKING
    * booking_created → POST_BOOKING
    * is_cancel → CANCEL_FLOW
    * is_admin → ADMIN_HANDOFF

File 3: tests/unit/test_slot_tracker.py — Unit tests:
  - Test every phase transition
  - Test empty slots → GREETING
  - Test partial slots → correct intermediate phase
  - Test slot-skipping (all fields at once → CONFIRMATION)
  - Test backward compat: SlotValues() with no new fields → GREETING

Do NOT touch any other files. Do NOT modify cancel_flow.py, schedule_flow.py, idempotency.py.
```

### Промпт 3.1.2 — Knowledge Base extensions
```
Read RFC_003_CONVERSATION_ENGINE_V2.md section 8 ("Knowledge Base — дополнения").

Task: Add new sections to knowledge/studio.yaml and update the KB validator.

Step 1 — PLAN ONLY:
- Show the new YAML sections you will add
- Show which validation checks you will add to knowledge/base.py
- Confirm you will NOT modify existing sections (studio, tone, services, teachers, faq, holidays, escalation)

Step 2 — After approval:

File 1: knowledge/studio.yaml — Add these NEW sections (keep all existing sections):
  - branches: list of {id, name, address, styles[]}
    * Get real branch data from the existing studio section or use the data from RFC-003 §8.1
  - style_recommendations: {feminine_heels: [...], feminine_sneakers: [...], energetic: [...]}
  - dress_code: dict of style_id → dress code string
  - promotions: [] (empty list for now — RFC-003 OQ-V2-3)

File 2: knowledge/base.py — Extend validation:
  - Validate branches section exists and has at least 1 branch
  - Each branch must have: id, name, address, styles (non-empty list)
  - Validate dress_code section exists
  - Validate style_recommendations section exists
  - promotions can be empty list
  - Add accessor methods:
    * get_branch(name_or_id: str) → branch dict | None
    * get_dress_code(style: str) → str | None  
    * get_branch_address(branch_name: str) → str | None
    * get_active_promotions() → list[dict]

File 3: tests/unit/test_kb_extensions.py — Tests for new accessors

Do NOT modify existing KB accessor methods. Do NOT break existing validation.
```

### Промпт 3.1.3 — Prompt Builder
```
Read RFC_003_CONVERSATION_ENGINE_V2.md sections 5.1–5.6 ("Системный промпт").

Task: Create the prompt builder that assembles the system prompt dynamically.

Step 1 — PLAN ONLY:
- Show the module structure
- Show how you inject: role, sales rules, current slots, KB data, tools, constraints
- Show the LLMResponse pydantic model

Step 2 — After approval:

File 1: app/core/prompt_builder.py — NEW file, under 150 lines:

  class PromptBuilder:
      def __init__(self, kb: KnowledgeBase)
      
      def build_system_prompt(
          self, 
          slots: SlotValues,
          phase: ConversationPhase,
          schedule_data: list | None = None,  # Pre-fetched CRM schedule
      ) -> str
      
      # Internal methods:
      # _role_and_tone() → str                     # From RFC-003 §5.2
      # _sales_rules() → str                        # From RFC-003 §5.3 (ALL 10 rules)
      # _format_slots_context(slots, phase) → str    # From RFC-003 §5.4
      # _format_kb_context(slots, phase) → str       # Branch addresses, style descriptions, dress code, promos
      # _format_tools() → str                        # From RFC-003 §5.5
      # _constraints() → str                         # From RFC-003 §5.6
      
  The sales rules in §5.3 are CRITICAL — copy them exactly into the prompt.
  These are proven conversion rules from the studio owner's sales scripts.

File 2: app/core/prompt_builder.py — Also define:
  
  class ToolCall(BaseModel):
      name: str
      parameters: dict[str, Any] = {}
  
  class LLMResponse(BaseModel):
      message: str
      slot_updates: dict[str, Any] = {}
      tool_calls: list[ToolCall] = []
      intent: str = "continue"  # "continue"|"booking"|"cancel"|"escalate"|"info"

File 3: tests/unit/test_prompt_builder.py:
  - Test that system prompt includes all 10 sales rules
  - Test slots injection format
  - Test KB context includes branch addresses when branch is set
  - Test tools section is present
  - Test prompt length is reasonable (< 4000 tokens estimate)
  
Do NOT implement the LLM call itself — only the prompt assembly.
Do NOT import or modify booking_flow.py, intent.py, or fsm.py.
```

### Промпт 3.1.4 — Guardrails
```
Read RFC_003_CONVERSATION_ENGINE_V2.md section 6 ("Guardrails").

Task: Create the guardrail system that validates every LLM response before sending to client.

Step 1 — PLAN ONLY:
- List all 12 guardrails (G1-G12) with their trigger conditions
- Show the GuardrailResult model
- Show the retry logic flow

Step 2 — After approval:

File 1: app/core/guardrails.py — NEW file, under 200 lines:

  class GuardrailResult(BaseModel):
      passed: bool
      violations: list[str] = []
      corrected_message: str | None = None  # For auto-fix guardrails (G7, G8)
  
  class GuardrailRunner:
      def __init__(self, kb: KnowledgeBase)
      
      async def check(
          self,
          llm_response: LLMResponse,
          slots: SlotValues,
          phase: ConversationPhase,
          crm_schedule: list | None = None,
      ) -> GuardrailResult
  
  Implement these checks per RFC-003 §6.1:
  
  HARD BLOCKS (return violations, block response):
  - G1: If response mentions specific times/days AND crm_schedule is available
        → verify times exist in crm_schedule
  - G2: If response contains price (regex: \d+\s*₽) → verify against KB prices
  - G3: If intent="booking" or tool_call="create_booking" 
        → verify all required slots filled (group, datetime_resolved, client_name, client_phone)
  - G4: If tool_call="create_booking" → verify slots.confirmed == True
  - G5: If response contains comparison words + teacher names → block
  - G6: (Handled externally — tool execution failure → forced fallback text)
  - G9: If phase=POST_BOOKING → verify response contains address substring AND dress_code substring
  - G10: If tool_call="create_booking" with schedule_id → verify schedule_id in crm_schedule
  - G11: If slot_updates contains datetime → verify it's in the future
  - G12: If response mentions schedule/time AND no tool_call for get_filtered_schedule → block

  AUTO-FIX (correct and pass):
  - G7: If len(message) > 300 → truncate at last sentence boundary ≤ 300
  - G8: Count emoji, if > 2 → strip extras from the end

File 2: tests/unit/test_guardrails.py:
  - Test each guardrail individually with crafted LLMResponse objects
  - Test G7 truncation preserves sentence boundaries
  - Test G8 emoji stripping
  - Test G3 blocks booking without phone
  - Test G12 blocks schedule hallucination (no tool_call)
  - Test passing response returns GuardrailResult(passed=True)

Do NOT implement the retry loop yet — that's in the engine.
```

---

## Phase 3.2: Engine + Интеграция

### Промпт 3.2.1 — Conversation Engine
```
Read RFC_003_CONVERSATION_ENGINE_V2.md section 7 ("Conversation Engine").

Task: Create the main conversation engine that replaces booking_flow.py.

IMPORTANT CONTEXT — read these existing files first:
- app/core/schedule_flow.py (you will call generate_schedule_response)
- app/core/cancel_flow.py (you will delegate cancel intent here)
- app/core/booking_confirm.py (you will call confirm_booking)
- app/core/conversation.py (you will use get_or_create_session, save_session_to_store, update_slots)
- app/core/slot_tracker.py (you just created this — compute_phase)
- app/core/prompt_builder.py (you just created this — PromptBuilder)
- app/core/guardrails.py (you just created this — GuardrailRunner)
- app/ai/router.py (existing LLM router — use for generation)

Step 1 — PLAN ONLY:
- Show the ConversationEngine class with all method signatures
- Show the message handling flow (RFC-003 §7.1 steps 1-10)
- Show how you delegate to existing modules
- Show how the guardrail retry loop works (max 2 retries)
- Confirm you do NOT modify schedule_flow.py, cancel_flow.py, or idempotency.py

Step 2 — After approval:

File: app/core/engine.py — NEW file, under 250 lines:

  class ConversationEngine:
      def __init__(self, llm_router, prompt_builder, guardrails, impulse_adapter, kb)
      
      async def handle_message(self, message: UnifiedMessage, trace_id: UUID) -> str:
          """Main entry point. Replaces BookingFlow.process_message()."""
          
          # 1. Load session (existing get_or_create_session)
          # 2. Compute phase from slots (slot_tracker.compute_phase)
          # 3. DELEGATE special phases:
          #    - CANCEL_FLOW → cancel_flow.get_cancel_flow() — existing code, untouched
          #    - ADMIN_HANDOFF → existing escalation logic
          #    - BOOKING (confirmed=True, not yet created) → booking_confirm.confirm_booking()
          # 4. For normal phases: build prompt (prompt_builder)
          # 5. Pre-fetch schedule from CRM if phase >= SCHEDULE
          # 6. LLM generate → parse LLMResponse (structured JSON)
          # 7. Execute tool_calls:
          #    - "get_filtered_schedule" → schedule_flow.generate_schedule_response()
          #    - "search_kb" → kb.search()
          #    - "create_booking" → booking_confirm.confirm_booking()
          #    - "start_cancel_flow" → cancel_flow.start()
          #    - "escalate_to_admin" → escalation
          # 8. Guardrail check → retry if failed (max 2)
          # 9. Apply slot_updates to session
          # 10. Save session + return response
      
      # Handle /start and /debug commands (from existing booking_flow.py logic)
      async def _handle_start(self, session) -> str
      async def _handle_debug(self, session) -> str
      
      # Safe fallback when guardrails fail after max retries
      def _safe_fallback(self, phase: ConversationPhase) -> str
      
      # Response length enforcement (from existing booking_flow.py)
      def _enforce_length(self, text: str, channel: str) -> str

  Key behaviors:
  - Cancel flow: if phase == CANCEL_FLOW, delegate ENTIRELY to cancel_flow.py
    Use the SAME logic as current booking_flow.py lines for CANCEL_FLOW state.
  - Confirmation: if user says "да" and phase == CONFIRMATION, call confirm_booking directly.
    Do NOT send to LLM for simple yes/no — fast path, same as current _handle_confirmation.
  - History: append user/assistant messages to slots.messages, keep last 10.
  - Booking receipt: after confirm_booking succeeds, set slots.booking_created = True.

Do NOT delete booking_flow.py yet — we'll do that after integration testing.
Do NOT modify cancel_flow.py, schedule_flow.py, booking_confirm.py, idempotency.py.
```

### Промпт 3.2.2 — Обогащение receipt
```
Read RFC_003_CONVERSATION_ENGINE_V2.md section 9.1 (receipt requirements) and section 6.1 (Guardrail G9).

Task: Update booking_confirm.py to include branch address and dress code in receipt.

Read these files first:
- app/core/booking_confirm.py (current receipt generation)
- knowledge/studio.yaml (branch addresses and dress_code you just added)

Update app/core/booking_confirm.py — function generate_receipt():

CURRENT receipt format:
  ✅ Запись подтверждена!
  Направление: {group}
  Дата и время: {datetime}
  Имя: {name}
  Телефон: {phone}
  Адрес: {studio_address}   ← currently uses generic studio.address

NEW receipt format:
  ✅ Запись подтверждена!
  Направление: {group}
  Дата и время: {datetime}
  Имя: {name}
  Телефон: {phone}
  Адрес: {branch_address}   ← from KB branches by session.slots.branch
  С собой: {dress_code}     ← from KB dress_code by session.slots.group

Implementation:
1. Get branch address: kb.get_branch_address(session.slots.branch) or fallback to kb.studio.address
2. Get dress code: kb.get_dress_code(session.slots.group) or "" if not found
3. Append dress code line ONLY if non-empty
4. Keep the 300 char truncation logic
5. Keep the LLM warm closing at the end

Do NOT change confirm_booking() logic — only generate_receipt().
Do NOT touch cancel_flow.py, schedule_flow.py, idempotency.py.
```

### Промпт 3.2.3 — Wire engine to main.py
```
Read app/main.py — find where BookingFlow.process_message() is called on webhook.

Task: Replace BookingFlow with ConversationEngine as the message handler.

Step 1 — PLAN ONLY:
- Show which import changes
- Show which lines in the webhook handler change  
- Show how ConversationEngine is instantiated (dependencies)
- Confirm that /health endpoint is NOT modified

Step 2 — After approval:

Changes to app/main.py:
1. Import ConversationEngine from app.core.engine instead of BookingFlow from app.core.booking_flow
2. In the lifespan/startup:
   - Instantiate PromptBuilder(kb)
   - Instantiate GuardrailRunner(kb)
   - Instantiate ConversationEngine(llm_router, prompt_builder, guardrails, impulse_adapter, kb)
3. In webhook handler: replace booking_flow.process_message(message, trace_id)
   with engine.handle_message(message, trace_id)
4. Keep ALL other endpoints untouched (/health, /webhook/whatsapp if exists)

Changes to app/core/conversation.py:
- Remove import of can_transition from fsm.py
- Remove import of ConversationState usage in transition_state() — replace with:
  * transition_state now just sets session.state to new_state string and saves
  * No validation matrix needed (engine controls transitions via compute_phase)
- Keep ALL session CRUD functions: get_or_create_session, save_session_to_store, 
  update_slots, reset_session, check_timeout, remove_session, recover_stale_sessions

Do NOT delete booking_flow.py or fsm.py yet.
Do NOT modify cancel_flow.py — it still uses transition_state and ConversationState, 
which still exist in models.py.
```

### Промпт 3.2.4 — Адаптировать conversation.py для совместимости
```
Read app/core/conversation.py (current version) and app/core/cancel_flow.py.

Problem: cancel_flow.py calls transition_state(session, ConversationState.IDLE).
The ConversationState enum currently lives in app/models.py (or core/fsm.py).
We want to delete fsm.py but keep ConversationState enum available for backward compat.

Task: Ensure ConversationState enum stays in app/models.py (if not already there)
and remove dependency on fsm.py's can_transition().

Step 1 — Check: is ConversationState defined in app/models.py or app/core/fsm.py?

Step 2 — If in fsm.py:
  - Move ConversationState enum to app/models.py
  - Update all imports across the codebase: cancel_flow.py, booking_confirm.py, conversation.py
  - Remove can_transition() usage from conversation.py (engine manages transitions now)
  - Keep get_timeout_seconds() logic — move to conversation.py or slot_tracker.py

Step 3 — Simplify transition_state in conversation.py:
  - Remove can_transition check
  - Just set session.state = new_state and save
  - This allows cancel_flow.py to continue working without changes

Step 4 — NOW delete app/core/fsm.py

Verify: cancel_flow.py still works — it calls transition_state(session, ConversationState.IDLE)
and that function still exists and works.
```

---

## Phase 3.3: Тесты + Очистка

### Промпт 3.3.1 — Новые prompt regression тесты
```
Read RFC_003_CONVERSATION_ENGINE_V2.md section 10 ("Prompt Regression Tests").

Task: Add new test suites for sales quality and update the runner.

File 1: tests/prompt_regression/test_sales_quality.yaml — NEW suite with 7 tests:

1. "help_choose_direction" — user: "Хочу на танцы, новичок, не знаю что выбрать"
   expected:
     contains_one_of: ["каблук", "кроссовк", "женственн", "энергичн"]
     not_contains: ["High Heels, Girly Hip-Hop, Frame Up Strip, Dancehall"]

2. "suggest_specific_date" — setup: {group: "High Heels", branch: "Семёновская"}
   user: "Когда можно прийти?"
   expected:
     tool_calls: ["get_filtered_schedule"]
     contains_one_of: ["ближайш", "предлож", "можно подойти", "запишем"]
     not_contains: ["Когда тебе удобно"]

3. "no_schedule_repeat" — setup: {group: "High Heels", schedule_shown: true}
   user: "Когда можно прийти?"
   expected:
     not_contains: ["Расписание"]

4. "receipt_has_address" — setup: {booking_created: true, group: "High Heels", branch: "Семёновская"}
   expected:
     contains: ["Семёновская"]
     contains_one_of: ["каблуки", "носочки"]

5. "one_question_per_message" — user: "Привет, хочу записаться"
   expected:
     max_question_marks: 1

6. "ask_branch" — user: "Хочу на High Heels"
   expected:
     contains_one_of: ["филиал", "удобнее заниматься", "Гоголя", "Семёновская"]

7. "no_spots_alternative" — crm_mock: {no_spots: true}
   user: "Запишите на пятницу"
   expected:
     contains_one_of: ["нет мест", "лист ожидания", "другую группу"]

File 2: Update tests/prompt_regression/runner.py if needed to support:
  - "setup" field with pre-filled slots
  - "contains_one_of" assertion (pass if ANY item found)
  - "max_question_marks" assertion
  - "crm_mock" field (for no-spots scenario)

Run: python -m tests.prompt_regression.runner
Verify all existing 20 tests still pass (≥ 90% threshold).
New suite: test_sales_quality should pass ≥ 5/7 on first try.
```

### Промпт 3.3.2 — Удаление старого кода
```
Task: Clean up — delete deprecated files and fix all remaining imports.

ONLY proceed with this if all tests pass (prompt regression ≥ 90%).

Delete these files:
1. app/core/booking_flow.py — replaced by engine.py
2. app/core/slot_collector.py — logic moved to LLM prompt
3. app/core/intent.py — intent is now part of LLM structured response

Then:
- Search entire codebase for imports from these deleted files
- Fix any remaining references
- Run: python -m tests.prompt_regression.runner → must pass ≥ 90%
- Run: docker compose up → must work
- Run: curl localhost:8000/health → must return OK

Do NOT delete cancel_flow.py, schedule_flow.py, booking_confirm.py, 
conversation.py, idempotency.py, temporal.py, or escalation.py.
```

### Промпт 3.3.3 — E2E smoke test
```
Task: Manual smoke test script that verifies the full flow works.

Create tests/e2e/test_engine_smoke.py:

Test 1 — Happy path booking:
  Simulate 6-8 messages:
  1. "Привет, хочу записаться на танцы" → should ask about branch or direction
  2. "На Семёновской" → should ask about direction (or if given, about experience)
  3. "High Heels, я новичок" → should call get_filtered_schedule, show options with specific date
  4. "Давайте в пятницу" → should ask for name and phone
  5. "Маша Иванова, 89241234567" → should show confirmation summary
  6. "Да" → should create booking, show receipt with address + dress code
  
  Verify:
  - Receipt contains "Семёновская 30а"
  - Receipt contains dress code text
  - Total messages ≤ 8
  - No schedule shown without filters
  - No repeated schedule display

Test 2 — Direction help:
  1. "Хочу на танцы, не знаю что выбрать" → should ask heels vs sneakers
  2. "На каблуках" → should suggest High Heels or Frame Up Strip (not full list)

Test 3 — Cancel flow still works:
  1. "Хочу отменить запись" → should delegate to cancel_flow.py
  
Use TEST_MODE=true with mock CRM.
Run with: python -m pytest tests/e2e/test_engine_smoke.py -v
```

---

## Если Cursor делает что-то не то

### Cursor трогает cancel_flow.py или schedule_flow.py
```
STOP. Do NOT modify cancel_flow.py or schedule_flow.py.
These modules work correctly and are called from engine.py as-is.
Read RFC-003 section 3.2: "cancel_flow.py — UNCHANGED, schedule_flow.py — UNCHANGED".
Revert your changes to these files.
```

### Cursor выдумывает CRM эндпоинты
```
STOP. The CRM API is defined in CONTRACT.md §5 and RFC_REFERENCE.md Appendix A.
Use ONLY the existing impulse adapter functions in app/integrations/impulse/.
Do not create new CRM endpoints or modify the adapter.
```

### Cursor пишет слишком сложный промпт
```
The system prompt must contain the 10 sales rules from RFC-003 §5.3 EXACTLY.
These are proven conversion rules from the real studio admin.
Do not simplify, rephrase, or skip any rule.
Do not add rules that are not in RFC-003.
```

### Cursor ломает prompt regression
```
Run: python -m tests.prompt_regression.runner
Show me the output. Which tests failed?
Do not proceed until existing tests pass ≥ 90%.
The new sales_quality tests can be tuned later — but existing 20 tests must not regress.
```

### Cursor забывает про guardrails
```
Re-read RFC-003 §6. Every LLM response MUST pass through GuardrailRunner.check()
BEFORE being sent to the user. This is a hard requirement.
The guardrail check happens in engine.py step [7], not in the prompt.
Guardrails are CODE enforcement, not prompt suggestions.
```

### Cursor пытается отправить сообщение напрямую (не через queue)
```
CONTRACT §9: ALL outgoing messages go through outbound queue → worker sends.
Do not call telegram.send_message() directly from engine.py.
Return the response text from handle_message(), and the caller (main.py webhook)
puts it in the outbound queue.
```

---

## Порядок выполнения (чеклист)

```
□ 0.1  Обновить cursorrules
□ 3.1.1 SlotValues + ConversationPhase + compute_phase + unit tests
□ 3.1.2 KB extensions (branches, dress_code, style_recommendations) + tests  
□ 3.1.3 Prompt Builder + LLMResponse model + tests
□ 3.1.4 Guardrails (12 checks) + tests
□ 3.2.1 ConversationEngine + handle_message
□ 3.2.2 Receipt enrichment (address + dress code)
□ 3.2.3 Wire engine to main.py
□ 3.2.4 Move ConversationState to models.py, delete fsm.py
□ 3.3.1 New prompt regression tests (7 sales quality tests)
□ 3.3.2 Delete old code (booking_flow, slot_collector, intent)
□ 3.3.3 E2E smoke test

Критерий готовности каждого шага:
- Unit тесты проходят
- docker compose up работает
- /health возвращает OK
- Prompt regression ≥ 90% (проверять после 3.2.3+)
```
