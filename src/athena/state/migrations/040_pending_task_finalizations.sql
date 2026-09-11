CREATE TABLE IF NOT EXISTS pending_task_finalizations (
    task_id TEXT PRIMARY KEY,
    intended_status TEXT NOT NULL,
    result_json TEXT NOT NULL,
    phase TEXT NOT NULL,
    observer_state TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_pending_task_finalizations_phase
    ON pending_task_finalizations(phase, updated_at);
