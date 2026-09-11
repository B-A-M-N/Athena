CREATE TABLE IF NOT EXISTS task_steers (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    source_task_id TEXT,
    source TEXT NOT NULL DEFAULT 'operator',
    principal_id TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    consumed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_task_steers_pending
    ON task_steers(task_id, status, created_at);
