CREATE TABLE IF NOT EXISTS workflow_runs (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    task_id TEXT,
    status TEXT NOT NULL,
    inputs TEXT NOT NULL,
    outputs TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_workflow_runs_task
    ON workflow_runs(task_id, updated_at);

CREATE TABLE IF NOT EXISTS workflow_step_runs (
    run_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    status TEXT NOT NULL,
    output TEXT,
    failures TEXT NOT NULL DEFAULT '[]',
    started_at TEXT,
    completed_at TEXT,
    PRIMARY KEY (run_id, step_id),
    FOREIGN KEY (run_id) REFERENCES workflow_runs(id) ON DELETE CASCADE
);
