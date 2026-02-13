# DanceBot — Cursor Contract v1.2

> **You are an implementation assistant. Follow this contract strictly.**
> **If unclear — ASK. Do not invent.**
> **Details: see `RFC_REFERENCE.md`. This contract overrides RFC on conflicts.**

---

## 1. What we're building

Chatbot backend for a dance studio: answers questions from KB/CRM, books classes, sends reminders, escalates to human. Never invents facts.

**Channels:** Telegram (Phase 1), WhatsApp (Phase 2). No Instagram in MVP.

## 2. Do NOT implement

Payments. Admin panel UI. Multi-tenant. Voice processing. Recommendation engine. Microservices. Kafka/RabbitMQ. Long-term personalization.

## 3. Architecture

**Monolith + worker. 5 containers. Docker Compose.**

```
app      → FastAPI: webhooks, FSM, LLM, CRM tools
worker   → Outbound queue, reminders, retries, DLQ
redis    → Sessions, locks, cache, queues, budget counters
postgres → Message logs, audit, booking attempts, errors, LLM cost
caddy    → HTTPS reverse proxy, auto Let's Encrypt
```

- `api ↔ worker` communicate via Redis lists
- Target: Yandex Cloud VM, 2 vCPU / 4 GB / 40 GB SSD
- Deploy: `docker compose up -d` — that's it

## 4. Data ownership

| Data | Source of truth | Storage |
|------|----------------|---------|
| Schedule, bookings, clients | **CRM (Impulse)** | CRM API, cached in Redis |
| Conversation state + slots | **Redis** | TTL 24h |
| User profiles | **Redis** | TTL 90d |
| Prices, FAQ, studio info | **KB file** | `knowledge/studio.yaml` |
| Logs, metrics, audit | **Postgres** | Retention 90d |

**Bot MUST NOT invent schedule, prices, or availability.**

## 5. CRM: Impulse CRM

```
Auth:   HTTP Basic (API key, permanent, no session management)
URL:    POST https://{tenant}.impulsecrm.ru/api/{entity}/{action}
Actions: list, load, update (create), update+id (modify), delete
```

Key entities: `schedule`, `reservation`, `client`, `group`, `teacher`, `hall`, `style`.
Request/response format: see RFC Appendix A.

**Required functions:** `get_schedule`, `get_groups`, `find_client`, `create_client`, `create_booking`, `list_bookings`, `cancel_booking`, `health_check`.

**CRM errors → user-friendly message + fallback queue. See RFC §10.4.**

## 6. Conversation rules (hard)

- One question per message
- Before booking: **mandatory summary + explicit confirmation**
- After booking: receipt (date/time/group/address)
- If no data in KB/CRM → "I'll check with admin" — **NEVER guess**
- No "As an AI…", no corporate jargon, no emoji spam (max 2)
- Response length: TG ≤ 300 chars, WA ≤ 200 chars

## 7. FSM

**Deterministic FSM with slot filling.** States:

```
IDLE → COLLECTING_INTENT → BROWSING_SCHEDULE
                         → COLLECTING_GROUP → COLLECTING_DATETIME
                           → COLLECTING_CONTACT → CONFIRM_BOOKING
                             → BOOKING_IN_PROGRESS → BOOKING_DONE → IDLE
CANCEL_FLOW    (show future bookings → select → confirm)
SERIAL_BOOKING (batch up to 5 dates)
HANDOFF_TO_ADMIN → ADMIN_RESPONDING → IDLE
```

**Required booking slots:** group, datetime, client_name, client_phone, confirmation (explicit yes).
**Auto-fill:** phone from WhatsApp. Timezone = Asia/Vladivostok.

**Timeouts:** session 24h → IDLE. CONFIRM_BOOKING 1h → re-prompt, 3h → IDLE. BOOKING_IN_PROGRESS 30s → fallback. ADMIN 4h → IDLE.

**Competing events:** topic change mid-booking → answer, return to flow. Message during BOOKING_IN_PROGRESS → buffer. During ADMIN_RESPONDING → relay to admin.

**Full transition table + temporal parser rules: see RFC §7–8.**

## 8. Inbound messages

**UnifiedMessage** must have: channel, chat_id, message_id, timestamp, text, message_type, sender_phone.

- **Dedup:** `SETNX seen:{channel}:{message_id}` TTL 5min. If exists → drop.
- **Non-text** (voice/sticker/image): reply "I only understand text 😊" — do NOT pass to LLM.
- **Edited message** (TG): treat as slot correction, don't reset FSM.
- **Spam:** >5 msg / 10s from same chat → drop silently.

## 9. Outbound messages

**ALL outgoing messages go through Redis queue → worker sends.**

- Rate limit: TG 30/s, WA 80/s
- Retry: 0s → 5s → 30s → DLQ (Postgres `dead_letter_messages`)
- DLQ > 10 → alert admin
- Reminder SLA: 24h ±30min, 2h ±5min

## 10. Idempotency

```
fingerprint = sha256(phone + schedule_id)
key = idempotency:{fingerprint}  TTL 10min
```

Lock set BEFORE CRM call. On duplicate: "You're already booked ✅". **One booking only, even under retries.**

## 11. LLM rules

**Allowed:** intent classification, slot extraction, rewriting messages naturally, answering FAQ from KB data.

**Forbidden:** inventing facts, generating schedule/prices without tool data, comparing teachers, answering schedule/booking questions without tool call.

**Hard rules (enforced in code, not just prompt):**

| Rule | Enforcement |
|------|-------------|
| Schedule/booking → require tool_call | Policy Enforcer |
| Price in response → must match KB | Policy Enforcer |
| Summary before booking | FSM: CONFIRM_BOOKING mandatory |
| Tool failed → "checking with admin" | Policy Enforcer |
| Invalid JSON from LLM | 3-step parser: parse → extract code block → retry → fallback to None |

**App MUST NOT crash on bad LLM output.**

## 12. Budget Guard

```
MAX_TOKENS_PER_HOUR:    100,000
MAX_COST_PER_DAY_USD:   10.0
MAX_REQUESTS_PER_MINUTE: 30
MAX_ERRORS_PER_HOUR:    50
```

On breach: alert admin TG → switch to static KB mode (no LLM) → bookings via fallback queue.

## 13. Degradation

| Level | Cause | Behavior |
|-------|-------|----------|
| L0 | All up | Full functionality |
| L1 CRM down | 5xx/timeout | Consult from KB. Bookings → fallback queue. |
| L2 LLM down | Budget/outage | Static KB mode. Bookings → fallback. |
| L3 Both down | CRM + LLM | "Technical issues, admin will contact you." |

**Data NEVER lost.** Everything goes to fallback queue.

## 14. Human Handoff

```
Bot → client: "Passing to admin ⏳"
Bot → admin TG chat: context + "/reply {chat_id} your answer"
Admin: "/reply 12345 Sure, we can reschedule"
Bot → client: "Admin: Sure, we can reschedule"
Admin: "/close 12345" → IDLE
```

All client messages relayed while in ADMIN_RESPONDING.

## 15. Knowledge Base

File: `knowledge/studio.yaml`. Schema v1.0.
Required sections: studio, tone, services, teachers, faq, holidays, escalation.
**No `rating` field for teachers.**

- Validated on startup. Invalid → app refuses to start.
- Updated by editing YAML + restarting app.
- KB vs CRM conflict: CRM wins for schedule, KB wins for prices.

**Schema details: see RFC §11.**

## 16. WhatsApp templates

Reminders (24h, 2h) and out-of-window confirmations require pre-approved Meta templates.
If template not approved → WhatsApp reminders blocked.
Fallback: send via TG if client has TG session.

**Template texts: see RFC §19.**

## 17. Observability

**Postgres tables:** messages, booking_attempts, tool_calls, llm_calls, errors, dead_letter_messages.
**Every conversation gets `trace_id` (UUID) at inbound.** Passes through all components.

**Test mode** (`TEST_MODE=true`): CRM mocked, full trace stdout, TG commands: `/debug`, `/trace {id}`, `/reset`, `/budget`.

## 18. Logging & Privacy

- **Log:** trace_id, FSM transitions, LLM metrics, CRM calls, message text.
- **NEVER log:** API keys, passwords, CRM credentials, full raw_payload.
- **Mask:** phone → `+7999****567`, email → `m***@mail.ru`.

## 19. Security (mandatory)

- Webhook signature verification (TG secret_token, WA X-Hub-Signature-256)
- Replay protection: timestamp window 5min + message_id dedup
- Secrets in `.env` (dev) / Yandex Lockbox (prod)
- Redis requirepass, Postgres password, network isolation
- HTTPS via Caddy
- User messages in LLM `user` role only, never `system`

## 20. Session Recovery

On startup: scan Redis sessions. BOOKING_IN_PROGRESS > 1min → fallback + notify client → IDLE. Any state > 24h → IDLE. ADMIN_RESPONDING > 4h → notify → IDLE.

## 21. Prompt Regression Tests

YAML test suites: user input → expected (contains/not_contains/tool_calls).
Run: `python -m tests.prompt_regression.runner`. Stability: temperature=0, 3 runs, pass if 2/3. Suite threshold: ≥ 90%.

**Run before every deploy. < 90% → deploy blocked.**

## 22. Endpoints

```
POST /webhook/telegram
POST /webhook/whatsapp
GET  /webhook/whatsapp    (verification)
GET  /health              {status, redis, crm}
```

## 23. Acceptance Criteria

- [ ] Schedule query → real data from CRM/KB
- [ ] Booking E2E in ≤ 8 messages avg
- [ ] Idempotent bookings (no duplicates)
- [ ] CRM down → fallback + admin alert
- [ ] LLM down → static KB mode works
- [ ] Reminders queued by worker
- [ ] All conversations in Postgres
- [ ] Budget Guard triggers correctly
- [ ] Prompt regression ≥ 90%
- [ ] Non-text messages handled
- [ ] `docker compose up` works

## 24. Tech constraints

Python 3.11+, FastAPI, Redis, PostgreSQL, httpx, structlog, pydantic v2. No Kafka. No microservices. Clean, testable code.

## 25. Working style

Before coding any module:
1. Propose file structure + plan
2. List which contract sections are satisfied
3. Only then implement

When modifying existing code: explain what and why, confirm no test breakage.

**If uncertain — ASK. Do not invent.**

---

*Contract v1.2 — source of truth. For details see `RFC_REFERENCE.md`.*
