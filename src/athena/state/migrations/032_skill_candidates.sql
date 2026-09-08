CREATE TABLE IF NOT EXISTS skill_candidates (
    id TEXT PRIMARY KEY,
    source_task_id TEXT NOT NULL,
    name TEXT NOT NULL,
    draft TEXT NOT NULL,
    rationale TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL DEFAULT 0,
    lifecycle_state TEXT NOT NULL DEFAULT 'PENDING_REVIEW',
    promoted_skill_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata TEXT
);
CREATE INDEX IF NOT EXISTS idx_skill_candidates_state
    ON skill_candidates(lifecycle_state, updated_at);
CREATE INDEX IF NOT EXISTS idx_skill_candidates_source_task
    ON skill_candidates(source_task_id);
