-- =============================================================================
-- Migration 002: Fix sessions.fsm_state column default and existing data
--
-- Problem: Migration 001 set DEFAULT 'idle' (lowercase) but ConversationState
--          Python enum uses uppercase values (e.g. 'IDLE', 'COLLECTING_INTENT').
--          Any session row created before app code ran save_session would have
--          fsm_state = 'idle' which Pydantic cannot deserialize back to the enum.
--
-- Fix:
--   1. ALTER the column default to 'IDLE' (matches ConversationState.IDLE.value).
--   2. UPDATE any existing rows that have the wrong lowercase default.
--      (Safe: only affects rows written by the broken default, not app-written rows.)
-- =============================================================================

-- Fix the column default for all future INSERTs
ALTER TABLE sessions
    ALTER COLUMN fsm_state SET DEFAULT 'IDLE';

-- Fix any existing rows that have the lowercase default value.
-- App-written rows already have correct uppercase values from the Python enum.
UPDATE sessions
    SET fsm_state = 'IDLE'
    WHERE fsm_state = 'idle';
