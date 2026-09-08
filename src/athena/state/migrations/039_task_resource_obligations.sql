CREATE TABLE IF NOT EXISTS task_resource_obligations (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    state TEXT NOT NULL,
    ownership_identity TEXT,
    first_failed_at TEXT NOT NULL,
    last_attempt_at TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 1,
    last_error TEXT,
    proof TEXT NOT NULL DEFAULT '{}',
    resolved_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(task_id, resource_type, resource_id)
);

CREATE INDEX IF NOT EXISTS idx_task_resource_obligations_open
    ON task_resource_obligations(state, task_id);
