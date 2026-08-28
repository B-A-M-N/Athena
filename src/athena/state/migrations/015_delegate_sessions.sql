CREATE TABLE IF NOT EXISTS delegate_sessions (
    id TEXT PRIMARY KEY,
    delegate_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    session_id TEXT,
    remote_session_id TEXT,
    workspace_root TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    launch_signature TEXT NOT NULL,
    metadata TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_delegate_sessions_task
    ON delegate_sessions(task_id, last_seen_at);
