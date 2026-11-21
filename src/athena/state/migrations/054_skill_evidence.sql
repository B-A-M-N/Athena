-- Empirical skill reuse evidence. These rows qualify deterministic
-- selection and application; they never authorize capability execution.
CREATE TABLE IF NOT EXISTS skill_evidence (
    id TEXT PRIMARY KEY,
    skill_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    task_class TEXT NOT NULL,
    environment_fingerprint TEXT NOT NULL,
    evidence_kind TEXT NOT NULL,
    outcome TEXT NOT NULL,
    passed INTEGER,
    cancelled INTEGER NOT NULL DEFAULT 0,
    verified INTEGER,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_skill_evidence_skill
    ON skill_evidence(skill_id, version, task_class, environment_fingerprint, created_at);
CREATE INDEX IF NOT EXISTS idx_skill_evidence_task
    ON skill_evidence(task_id, created_at);
