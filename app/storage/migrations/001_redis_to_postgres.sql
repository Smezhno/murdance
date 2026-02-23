-- =============================================================================
-- Migration 001: Redis → PostgreSQL
-- Replaces all Redis data structures with durable Postgres tables.
--
-- CONTRACT §3:  postgres stores sessions, cache, queues, budget, locks
-- CONTRACT §8:  seen_messages with UNIQUE for inbound dedup
-- CONTRACT §9:  outbound_queue → worker; retry 0s→5s→30s→DLQ
-- CONTRACT §10: idempotency_locks via UNIQUE INSERT
-- RFC §7:       FSM sessions (state, slots, history, metadata, TTL per state)
-- RFC §14:      budget_counters per hour / per day sliding windows
-- RFC §17:      Existing tables (messages, booking_attempts, tool_calls,
--               llm_calls, errors, dead_letter_messages) are NOT touched here.
--
-- Existing tables: messages, booking_attempts, tool_calls, llm_calls,
--                  errors, dead_letter_messages — DO NOT ALTER.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- ENUM: outbound message status
-- CONTRACT §9 — all outgoing messages go through outbound_queue → worker
-- ---------------------------------------------------------------------------

CREATE TYPE outbound_status AS ENUM (
    'pending',    -- waiting to be picked up by worker
    'sending',    -- worker is currently attempting delivery
    'sent',       -- delivered successfully
    'failed',     -- all retries exhausted → moved to dead_letter_messages
    'cancelled'   -- cancelled before delivery (e.g. session reset)
);

-- ---------------------------------------------------------------------------
-- TABLE: sessions
-- Replaces: Redis HASH session:{chat_id}
-- CONTRACT §3, §20 — conversation state + slots; source of truth in Postgres
-- RFC §7.1 — FSM states + TTLs; RFC §7.4 — session recovery on startup
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS sessions (
    channel         VARCHAR(20)                 NOT NULL,
    chat_id         VARCHAR(255)                NOT NULL,

    -- FSM state (RFC §7.1) — must match ConversationState enum values (uppercase)
    fsm_state       VARCHAR(50)                 NOT NULL DEFAULT 'IDLE',

    -- Collected booking slots: group, datetime, client_name, client_phone,
    -- confirmation, third_party_name, comment, schedule_id, etc.
    -- Stored as JSONB for flexibility — validated in Python via Pydantic.
    slots           JSONB                       NOT NULL DEFAULT '{}',

    -- Last N messages for LLM context window (RFC §22 sliding window 20 msg)
    -- JSONB array: [{role, content, ts}]
    history         JSONB                       NOT NULL DEFAULT '[]',

    -- Miscellaneous runtime metadata: trace_id, last_intent, handoff admin id,
    -- relay_chat_id, processing_lock, pending_buffer, etc.
    metadata        JSONB                       NOT NULL DEFAULT '{}',

    -- Wallclock expiry — checked by session recovery on startup (RFC §7.4)
    -- and by background watchdog. NULL = no expiry (e.g. HANDOFF_TO_ADMIN).
    -- App MUST set expires_at on every FSM state transition. TTLs per state:
    --   CONFIRM_BOOKING=3h, BOOKING_IN_PROGRESS=30s, ADMIN states=4h,
    --   all others=24h.
    expires_at      TIMESTAMP WITH TIME ZONE,

    created_at      TIMESTAMP WITH TIME ZONE    NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP WITH TIME ZONE    NOT NULL DEFAULT NOW(),

    PRIMARY KEY (channel, chat_id)
);

-- Primary key already covers (channel, chat_id) lookups — no separate index needed.

-- Session recovery: find sessions that have timed out (RFC §7.4 startup scan)
CREATE INDEX IF NOT EXISTS idx_sessions_expires_at
    ON sessions (expires_at)
    WHERE expires_at IS NOT NULL;

-- Session recovery: find BOOKING_IN_PROGRESS sessions quickly
CREATE INDEX IF NOT EXISTS idx_sessions_fsm_state
    ON sessions (fsm_state);

-- ---------------------------------------------------------------------------
-- TABLE: idempotency_locks
-- Replaces: Redis SETNX idempotency:{fingerprint}
-- CONTRACT §10 — fingerprint = sha256(phone + schedule_id); PRIMARY KEY INSERT;
--                lock set BEFORE CRM call; app checks created_at < 10 min to
--                detect duplicates (10-min window enforced in code, not here)
-- RFC §9.5 — "You're already booked ✅" on duplicate; rows kept 24h for audit
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS idempotency_locks (
    -- sha256(phone + schedule_id) — hex-encoded, 64 chars
    -- Natural key: always accessed by fingerprint. Surrogate id not needed.
    fingerprint     CHAR(64)                    NOT NULL,

    -- Context stored for duplicate response generation
    channel         VARCHAR(20)                 NOT NULL,
    chat_id         VARCHAR(255)                NOT NULL,
    client_phone    VARCHAR(50)                 NOT NULL,
    schedule_id     VARCHAR(255)                NOT NULL,

    -- Result of the CRM call (populated after success)
    crm_reservation_id  VARCHAR(255),

    created_at      TIMESTAMP WITH TIME ZONE    NOT NULL DEFAULT NOW(),

    -- Enforces exactly-once semantics (CONTRACT §10)
    PRIMARY KEY (fingerprint)
);

-- Cleanup job looks up expired locks; no other index needed beyond UNIQUE
CREATE INDEX IF NOT EXISTS idx_idempotency_locks_created_at
    ON idempotency_locks (created_at);

-- ---------------------------------------------------------------------------
-- TABLE: crm_cache
-- Replaces: Redis HASH/STRING crm:{entity}:{key}
-- CONTRACT §4  — CRM data cached in PostgreSQL crm_cache
-- RFC §20.3    — schedule TTL 15min; groups/teachers TTL 1h
--                Force-refresh after booking. Stale → refresh or escalate.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS crm_cache (
    -- Cache key: e.g. "schedule:all", "groups:all", "teachers:all",
    --             "client:+79991234567", "reservation:list:{chat_id}"
    -- Natural key: always accessed by cache_key. Surrogate id not needed.
    cache_key       VARCHAR(500)                NOT NULL,

    -- Raw CRM JSON response, stored verbatim
    payload         JSONB                       NOT NULL,

    -- Absolute expiry timestamp (RFC §20.2 TTL policy)
    -- Worker / app checks: WHERE NOW() < expires_at
    expires_at      TIMESTAMP WITH TIME ZONE    NOT NULL,

    created_at      TIMESTAMP WITH TIME ZONE    NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP WITH TIME ZONE    NOT NULL DEFAULT NOW(),

    PRIMARY KEY (cache_key)
);

-- Hot path: PRIMARY KEY covers exact cache_key lookups.
-- Separate index on expires_at for cleanup and staleness checks.

-- Cleanup job: DELETE WHERE expires_at < NOW()
CREATE INDEX IF NOT EXISTS idx_crm_cache_expires_at
    ON crm_cache (expires_at);

-- ---------------------------------------------------------------------------
-- TABLE: outbound_queue
-- Replaces: Redis ZSET outbound:{channel}
-- CONTRACT §3  — api ↔ worker communicate via outbound_queue + LISTEN/NOTIFY
-- CONTRACT §9  — ALL outgoing messages go through this table → worker sends
--                Rate limit: TG 30/s, WA 80/s
--                Retry: 0s → 5s → 30s → DLQ (dead_letter_messages)
--                DLQ > 10 → alert admin
-- RFC §18.1    — priority queue; channel rate-limit; retry policy
-- RFC §18.4    — reminder SLA: 24h ±30min, 2h ±5min
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS outbound_queue (
    id              BIGSERIAL PRIMARY KEY,

    -- Target channel and recipient
    channel         VARCHAR(20)                 NOT NULL,
    chat_id         VARCHAR(255)                NOT NULL,

    -- Message content
    text            TEXT                        NOT NULL,

    -- Optional structured payload for channel-specific features
    -- e.g. {"buttons": [...]} for TG inline keyboard, {"template": "..."} for WA
    payload         JSONB,

    -- Linked trace_id for observability (CONTRACT §17)
    trace_id        UUID,

    -- Scheduling: NULL = send ASAP; set for reminders (RFC §4 S3, §18.4)
    scheduled_at    TIMESTAMP WITH TIME ZONE,

    -- Priority: 0 = highest, higher numbers = lower priority.
    -- Worker dequeues with: ORDER BY priority ASC, id ASC (FIFO within a tier).
    -- Convention: 0=interactive reply, 1=reminder, 2=system/admin notification.
    priority        SMALLINT                    NOT NULL DEFAULT 0,

    -- Delivery status (the outbound_status ENUM above)
    status          outbound_status             NOT NULL DEFAULT 'pending',

    -- Retry tracking (CONTRACT §9 retry policy: 0s → 5s → 30s → DLQ)
    attempts        SMALLINT                    NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMP WITH TIME ZONE    NOT NULL DEFAULT NOW(),
    last_error      TEXT,

    created_at      TIMESTAMP WITH TIME ZONE    NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP WITH TIME ZONE    NOT NULL DEFAULT NOW()
);

-- Worker polling: pending + due messages, ordered by priority then id
CREATE INDEX IF NOT EXISTS idx_outbound_queue_worker
    ON outbound_queue (channel, status, next_attempt_at, priority, id)
    WHERE status IN ('pending', 'sending');

-- Observability: look up all messages for a given conversation
CREATE INDEX IF NOT EXISTS idx_outbound_queue_chat
    ON outbound_queue (channel, chat_id);

-- Observability: look up by trace
CREATE INDEX IF NOT EXISTS idx_outbound_queue_trace_id
    ON outbound_queue (trace_id);

-- Cleanup: find old terminal-state rows
CREATE INDEX IF NOT EXISTS idx_outbound_queue_created_at
    ON outbound_queue (created_at);

-- ---------------------------------------------------------------------------
-- TABLE: budget_counters
-- Replaces: Redis INCR/EXPIRE budget:{provider}:{window}
-- CONTRACT §12 / RFC §14 — hard limits on tokens/cost/requests/errors
--   MAX_TOKENS_PER_HOUR:     100,000
--   MAX_COST_PER_DAY_USD:    10.0
--   MAX_REQUESTS_PER_MINUTE: 30
--   MAX_ERRORS_PER_HOUR:     50
-- Sliding window: app increments counter, checks against limit.
-- On breach: alert admin TG → static KB mode → bookings via fallback queue.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS budget_counters (
    -- LLM provider (e.g. "anthropic", "openai", or "all" for aggregated)
    provider        VARCHAR(50)                 NOT NULL,

    -- Window granularity: "minute", "hour", "day"
    -- Quoted because "window" is a reserved word in PostgreSQL.
    "window"        VARCHAR(10)                 NOT NULL
                    CHECK ("window" IN ('minute', 'hour', 'day')),

    -- Start of this window (truncated to the window boundary)
    -- e.g. for hour-window: DATE_TRUNC('hour', NOW())
    -- Natural composite key: always accessed by (provider, "window", window_start).
    window_start    TIMESTAMP WITH TIME ZONE    NOT NULL,

    -- Accumulated values for this window
    tokens_used     INTEGER                     NOT NULL DEFAULT 0,
    requests_count  INTEGER                     NOT NULL DEFAULT 0,
    errors_count    INTEGER                     NOT NULL DEFAULT 0,
    cost_usd        DECIMAL(10, 6)              NOT NULL DEFAULT 0.0,

    created_at      TIMESTAMP WITH TIME ZONE    NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP WITH TIME ZONE    NOT NULL DEFAULT NOW(),

    -- One row per (provider, window granularity, window start time)
    PRIMARY KEY (provider, "window", window_start)
);

-- Budget Guard reads current window on every LLM call — covered by PRIMARY KEY.
-- Separate index on window_start for efficient cleanup of old windows.

-- Cleanup: delete old windows
CREATE INDEX IF NOT EXISTS idx_budget_counters_window_start
    ON budget_counters (window_start);

-- ---------------------------------------------------------------------------
-- TABLE: seen_messages
-- Replaces: Redis SETNX seen:{channel}:{message_id}
-- CONTRACT §8  — inbound dedup via UNIQUE INSERT; INSERT ... ON CONFLICT → skip
-- RFC §17      — message_id dedup, TTL 5 min (replay protection window)
-- Security     — replay attack protection: timestamp window 5 min (CONTRACT §19)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS seen_messages (
    -- Composite key: same message_id can appear on different channels.
    -- Natural key: always accessed by (channel, message_id). Surrogate id not needed.
    channel         VARCHAR(20)                 NOT NULL,
    message_id      VARCHAR(255)                NOT NULL,

    -- Stored for observability / debugging (not used for dedup logic)
    chat_id         VARCHAR(255)                NOT NULL,
    received_at     TIMESTAMP WITH TIME ZONE    NOT NULL DEFAULT NOW(),

    -- Absolute expiry — cleanup job deletes WHERE expires_at < NOW()
    -- Default: 5 minutes (RFC §17, CONTRACT §19 replay window)
    expires_at      TIMESTAMP WITH TIME ZONE    NOT NULL
                    DEFAULT (NOW() + INTERVAL '5 minutes'),

    -- Enforces exactly-once inbound processing (CONTRACT §8)
    PRIMARY KEY (channel, message_id)
);

-- Cleanup: DELETE WHERE expires_at < NOW()
CREATE INDEX IF NOT EXISTS idx_seen_messages_expires_at
    ON seen_messages (expires_at);

-- Observability: look up all messages for a conversation
CREATE INDEX IF NOT EXISTS idx_seen_messages_channel_chat_id
    ON seen_messages (channel, chat_id);

-- =============================================================================
-- CLEANUP SQL
-- Run periodically by a background job (APScheduler, e.g. every 15 minutes).
-- None of these touch the existing audit/observability tables.
-- =============================================================================

-- -- Cleanup: idempotency locks older than 24h (audit trail retention).
-- -- The 10-minute duplicate-booking window is enforced in app code by checking
-- -- created_at < NOW() - INTERVAL '10 minutes' at query time — NOT here.
-- -- Rows are kept for 24h so admin can audit "was this a duplicate booking?".
-- DELETE FROM idempotency_locks
--     WHERE created_at < NOW() - INTERVAL '24 hours';

-- -- Cleanup: expired CRM cache entries (RFC §20.2)
-- DELETE FROM crm_cache
--     WHERE expires_at < NOW();

-- -- Cleanup: expired inbound dedup records (TTL 5 min, CONTRACT §8, §19)
-- DELETE FROM seen_messages
--     WHERE expires_at < NOW();

-- -- Cleanup: expired sessions (TTL 24h for most states, CONTRACT §20)
-- -- Only deletes rows where expires_at has passed AND state is not persistent.
-- -- HANDOFF_TO_ADMIN / ADMIN_RESPONDING are handled by session recovery (RFC §7.4).
-- DELETE FROM sessions
--     WHERE expires_at IS NOT NULL
--       AND expires_at < NOW()
--       AND fsm_state NOT IN ('handoff_to_admin', 'admin_responding');

-- -- Cleanup: old budget counter windows (keep last 7 days for trend analysis)
-- DELETE FROM budget_counters
--     WHERE window_start < NOW() - INTERVAL '7 days';

-- -- Cleanup: terminal outbound_queue rows older than 7 days
-- DELETE FROM outbound_queue
--     WHERE status IN ('sent', 'failed', 'cancelled')
--       AND created_at < NOW() - INTERVAL '7 days';
