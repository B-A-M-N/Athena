CREATE TABLE IF NOT EXISTS pack_hook_outbox (
    id TEXT PRIMARY KEY,
    pack_id TEXT NOT NULL,
    hook_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    task_id TEXT,
    session_id TEXT,
    payload TEXT NOT NULL,
    depth INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'PENDING',
    attempts INTEGER NOT NULL DEFAULT 0,
    dispatched_task_id TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(hook_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_pack_hook_outbox_status
    ON pack_hook_outbox(status, created_at);
