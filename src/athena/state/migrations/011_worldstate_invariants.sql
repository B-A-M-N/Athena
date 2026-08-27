CREATE TABLE IF NOT EXISTS invariants (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    description TEXT NOT NULL,
    definition TEXT NOT NULL,
    required INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invariant_results (
    id TEXT PRIMARY KEY,
    invariant_id TEXT NOT NULL,
    task_id TEXT,
    passed INTEGER NOT NULL,
    error TEXT,
    details TEXT NOT NULL,
    checked_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_invariants_task ON invariants(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_invariant_results_task
    ON invariant_results(task_id, checked_at);
