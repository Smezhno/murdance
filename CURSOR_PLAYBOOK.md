# Как работать с Cursor по DanceBot — пошаговый playbook

## Золотые правила

1. **Один модуль за раз.** Никогда не проси "сделай весь проект". Cursor начнёт галлюцинировать на третьем файле.
2. **Сначала план, потом код.** Всегда начинай с "propose structure, don't write code yet".
3. **Явные ссылки на контракт.** Не "сделай FSM" а "implement FSM per CONTRACT.md §7".
4. **Проверяй каждый модуль** перед тем как идти дальше.
5. **Не давай контекст целиком.** Cursor видит файлы в проекте — не копируй RFC в чат.

---

## Phase 1: Скелет

### Промпт 1.1 — структура проекта
```
Read CONTRACT.md sections 3 and 24.
Propose the complete file/folder structure for the project.
Do not write any code yet — only the tree and a one-line description per file.
```

### Промпт 1.2 — конфиги и модели
```
Read CONTRACT.md sections 4, 5, 8.
Implement:
- app/config.py (pydantic-settings, all env vars from CONTRACT §3)
- app/models.py (UnifiedMessage per CONTRACT §8, Session, BookingRequest)
Show plan first, then code.
```

### Промпт 1.3 — Redis + Postgres setup
```
Read CONTRACT.md section 3.
Create:
- docker-compose.yml (all 5 services: app, worker, redis, postgres, caddy)
- app/storage/redis.py (connection pool)
- app/storage/postgres.py (async connection, table creation)
- Postgres tables per CONTRACT §17 (messages, booking_attempts, tool_calls, llm_calls, errors, dead_letter_messages)
```

### Промпт 1.4 — KB loader
```
Read CONTRACT.md §15 and RFC_REFERENCE.md §11.
Implement knowledge/base.py:
- Load and validate studio.yaml against schema
- Fail-fast on invalid schema (app must not start)
- Search function for FAQ
- Create knowledge/studio.yaml with sample data for Tatyana's studio
```

### Промпт 1.5 — Channel Gateway (Telegram)
```
Read CONTRACT.md §8, §19, §22.
Implement:
- app/channels/base.py (ChannelProtocol)
- app/channels/telegram.py (aiogram 3.x webhook handler)
- app/channels/filters.py (voice/sticker/image → friendly text reply)
- app/channels/dedup.py (message_id dedup via Redis SETNX)
- POST /webhook/telegram endpoint in main.py
- GET /health endpoint
```

### Промпт 1.6 — FSM skeleton
```
Read CONTRACT.md §7.
Implement:
- app/core/fsm.py (ConversationState enum, all states from CONTRACT)
- app/core/conversation.py (session load/save from Redis, state transitions, timeout rules)
For now just the skeleton — intent resolution and LLM come next.
```

### Промпт 1.7 — LLM Router + Budget Guard
```
Read CONTRACT.md §11, §12.
Implement:
- app/ai/router.py (LLMRouter: provider selection, tool calling interface)
- app/ai/budget_guard.py (all 5 limits from CONTRACT §12, Redis counters, auto-shutdown)
- app/ai/policy.py (Policy Enforcer: hard rules table from CONTRACT §11)
- app/ai/json_parser.py (3-step JSON extraction, never crash)
- app/ai/providers/base.py (LLMProvider Protocol)
- app/ai/providers/anthropic.py (Claude via anthropic SDK)
```

### Промпт 1.8 — Temporal Parser
```
Read CONTRACT.md §7 "Datetime resolution" and RFC_REFERENCE.md CC-2.
Implement app/core/temporal.py:
- Resolve relative dates in CODE, not LLM
- Timezone: Asia/Vladivostok hardcoded
- Rules: "tomorrow", "next Wednesday", "on the 5th", past date rejection
- Return: TemporalResult(date, time, confidence, ambiguous)
```

---

## Phase 2: CRM + Booking

### Промпт 2.1 — Impulse Adapter
```
Read CONTRACT.md §5 and RFC_REFERENCE.md Appendix A.
Implement app/integrations/impulse/:
- client.py (httpx + Basic auth, retry with tenacity, circuit breaker)
- models.py (Pydantic strict models for schedule, reservation, client, group)
- cache.py (Redis cache: schedule 15min, groups 1h, teachers 1h)
- error_handler.py (CRM error codes → user messages per CONTRACT §5)
- fallback.py (Redis fallback queue + TG admin alert)
All 8 required functions from CONTRACT §5.
```

### Промпт 2.2 — Idempotency
```
Read CONTRACT.md §10.
Implement app/core/idempotency.py:
- fingerprint = sha256(phone + schedule_id)
- Redis SETNX before CRM call, TTL 10min
- On duplicate → return "already booked" response
```

### Промпт 2.3 — Full booking flow
```
Read CONTRACT.md §6, §7, §11.
Now wire everything together:
- Intent resolution via LLM (with tools)
- Slot filling through FSM states
- Policy Enforcer checks after LLM response
- Booking via Impulse adapter
- Idempotency guard
- Fallback on CRM error
Test with TG /debug command.
```

### Промпт 2.4 — Prompt regression tests
```
Read CONTRACT.md §21.
Create:
- tests/prompt_regression/test_booking_flow.yaml (happy path, 6 steps)
- tests/prompt_regression/test_schedule_query.yaml
- tests/prompt_regression/test_edge_cases.yaml (correction, topic change, past date)
- tests/prompt_regression/runner.py
Must work with: python -m tests.prompt_regression.runner
```

---

## Phase 3: Resilience

### Промпт 3.1 — Cancel flow
```
Read CONTRACT.md §7 (CANCEL_FLOW).
Implement cancel: list future bookings by phone → user selects → confirm → delete reservation.
Client does NOT know reservation_id — bot shows list.
```

### Промпт 3.2 — Human Handoff
```
Read CONTRACT.md §14.
Implement relay mode:
- /reply {chat_id} text — admin command
- /close {chat_id} — end handoff
- All client messages forwarded while ADMIN_RESPONDING
- Timeout 4h → auto-close
```

### Промпт 3.3 — Session Recovery
```
Read CONTRACT.md §20.
Implement app/core/session_recovery.py — runs on app startup.
Scan Redis, fix stale BOOKING_IN_PROGRESS, reset old sessions.
```

### Промпт 3.4 — Degradation
```
Read CONTRACT.md §13.
Implement degradation levels L0-L3.
When CRM down → static KB + fallback queue.
When LLM budget exceeded → keyword match from KB, no LLM calls.
```

---

## Phase 4: WhatsApp + Outbound

### Промпт 4.1 — Worker process
```
Read CONTRACT.md §9.
Implement worker.py:
- Consume from Redis outbound:{channel} lists
- Rate limiting per channel
- Retry policy (0s → 5s → 30s → DLQ)
- DLQ → Postgres dead_letter_messages
- Reminder scheduler (24h + 2h before class)
```

### Промпт 4.2 — WhatsApp channel
```
Read CONTRACT.md §8, §16.
Implement app/channels/whatsapp.py:
- Cloud API via httpx
- Signature verification (X-Hub-Signature-256)
- Template messages for reminders
- Phone extraction from sender
```

---

## Если Cursor делает что-то не то

### Cursor выдумывает эндпоинты CRM
```
Stop. The CRM API is defined in CONTRACT.md §5 and RFC_REFERENCE.md Appendix A.
Use ONLY those endpoints. Do not invent new ones.
```

### Cursor пишет слишком длинный файл
```
This file is over 300 lines. Split it into smaller modules per CONTRACT §24.
Propose how to split, then refactor.
```

### Cursor добавляет не те зависимости
```
Check CONTRACT.md §24: only Python 3.11+, FastAPI, Redis, PostgreSQL, httpx, structlog, pydantic v2.
Remove {X} and use {Y} instead.
```

### Cursor забывает про правило
```
Re-read CONTRACT.md §{N}. Your implementation violates: "{конкретное правило}".
Fix it.
```
