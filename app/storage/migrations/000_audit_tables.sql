-- =============================================================================
-- Migration 000: Audit / observability tables
-- CONTRACT §17: messages, booking_attempts, tool_calls, llm_calls,
--               errors, dead_letter_messages
--
-- These tables were originally created as Python DDL in create_audit_tables().
-- Moved here so there is ONE migration mechanism (run_migrations) for all DDL.
-- All statements are idempotent (IF NOT EXISTS).
-- =============================================================================

CREATE TABLE IF NOT EXISTS messages (
    id           BIGSERIAL PRIMARY KEY,
    trace_id     UUID                     NOT NULL,
    channel      VARCHAR(20)              NOT NULL,
    chat_id      VARCHAR(255)             NOT NULL,
    message_id   VARCHAR(255)             NOT NULL,
    timestamp    TIMESTAMP WITH TIME ZONE NOT NULL,
    text         TEXT                     NOT NULL,
    message_type VARCHAR(20)              NOT NULL,
    sender_phone VARCHAR(50),
    sender_name  VARCHAR(255),
    direction    VARCHAR(10)              NOT NULL CHECK (direction IN ('inbound', 'outbound')),
    created_at   TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    UNIQUE (channel, message_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_trace_id        ON messages (trace_id);
CREATE INDEX IF NOT EXISTS idx_messages_channel_chat_id ON messages (channel, chat_id);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp       ON messages (timestamp);

-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS booking_attempts (
    id            BIGSERIAL PRIMARY KEY,
    trace_id      UUID                     NOT NULL,
    channel       VARCHAR(20)              NOT NULL,
    chat_id       VARCHAR(255)             NOT NULL,
    group_id      VARCHAR(255),
    schedule_id   VARCHAR(255),
    datetime      TIMESTAMP WITH TIME ZONE,
    client_name   VARCHAR(255),
    client_phone  VARCHAR(50),
    success       BOOLEAN                  NOT NULL,
    error_message TEXT,
    crm_response  JSONB,
    created_at    TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_booking_attempts_trace_id        ON booking_attempts (trace_id);
CREATE INDEX IF NOT EXISTS idx_booking_attempts_channel_chat_id ON booking_attempts (channel, chat_id);
CREATE INDEX IF NOT EXISTS idx_booking_attempts_success         ON booking_attempts (success);

-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS tool_calls (
    id          BIGSERIAL PRIMARY KEY,
    trace_id    UUID                     NOT NULL,
    tool_name   VARCHAR(100)             NOT NULL,
    parameters  JSONB                    NOT NULL,
    result      JSONB,
    error       TEXT,
    duration_ms INTEGER,
    created_at  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_trace_id   ON tool_calls (trace_id);
CREATE INDEX IF NOT EXISTS idx_tool_calls_tool_name  ON tool_calls (tool_name);
CREATE INDEX IF NOT EXISTS idx_tool_calls_created_at ON tool_calls (created_at);

-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS llm_calls (
    id                BIGSERIAL PRIMARY KEY,
    trace_id          UUID                     NOT NULL,
    provider          VARCHAR(50)              NOT NULL,
    model             VARCHAR(100)             NOT NULL,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    total_tokens      INTEGER,
    cost_usd          DECIMAL(10, 6),
    request_json      JSONB,
    response_json     JSONB,
    error             TEXT,
    duration_ms       INTEGER,
    created_at        TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_trace_id   ON llm_calls (trace_id);
CREATE INDEX IF NOT EXISTS idx_llm_calls_provider   ON llm_calls (provider);
CREATE INDEX IF NOT EXISTS idx_llm_calls_created_at ON llm_calls (created_at);

-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS errors (
    id            BIGSERIAL PRIMARY KEY,
    trace_id      UUID,
    error_type    VARCHAR(100)             NOT NULL,
    error_message TEXT                     NOT NULL,
    stack_trace   TEXT,
    context       JSONB,
    created_at    TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_errors_trace_id   ON errors (trace_id);
CREATE INDEX IF NOT EXISTS idx_errors_error_type ON errors (error_type);
CREATE INDEX IF NOT EXISTS idx_errors_created_at ON errors (created_at);

-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS dead_letter_messages (
    id              BIGSERIAL PRIMARY KEY,
    trace_id        UUID,
    channel         VARCHAR(20)              NOT NULL,
    chat_id         VARCHAR(255)             NOT NULL,
    text            TEXT                     NOT NULL,
    error           TEXT                     NOT NULL,
    attempts        INTEGER                  NOT NULL DEFAULT 1,
    last_attempt_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_dlq_trace_id   ON dead_letter_messages (trace_id);
CREATE INDEX IF NOT EXISTS idx_dlq_channel    ON dead_letter_messages (channel);
CREATE INDEX IF NOT EXISTS idx_dlq_created_at ON dead_letter_messages (created_at);
