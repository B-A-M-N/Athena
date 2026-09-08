CREATE TABLE IF NOT EXISTS context_digests (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    session_id TEXT,
    principal_id TEXT NOT NULL,
    level INTEGER NOT NULL,
    fields TEXT NOT NULL,
    transcript_anchors TEXT NOT NULL DEFAULT '[]',
    recovery_queries TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_context_digests_task
    ON context_digests(task_id, level DESC, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_context_digests_session
    ON context_digests(principal_id, session_id, level DESC, updated_at DESC);
