ALTER TABLE pack_hook_outbox ADD COLUMN hook_task_id TEXT;
ALTER TABLE pack_hook_outbox ADD COLUMN next_attempt_at TEXT;
CREATE INDEX IF NOT EXISTS idx_pack_hook_outbox_retry
    ON pack_hook_outbox(status, next_attempt_at, created_at);
