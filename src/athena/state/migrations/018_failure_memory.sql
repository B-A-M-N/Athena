CREATE TABLE IF NOT EXISTS failure_memory (
    id TEXT PRIMARY KEY,
    signature_fingerprint TEXT NOT NULL,
    capability_id TEXT NOT NULL,
    environment_fingerprint TEXT NOT NULL DEFAULT '',
    project_scope TEXT NOT NULL DEFAULT '',
    strategy TEXT NOT NULL,
    remediation TEXT,
    evidence_ids TEXT NOT NULL DEFAULT '[]',
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    last_success TEXT,
    last_failure TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_failure_memory_signature
    ON failure_memory(signature_fingerprint, capability_id);
CREATE INDEX IF NOT EXISTS idx_failure_memory_scope
    ON failure_memory(project_scope, environment_fingerprint, updated_at);
