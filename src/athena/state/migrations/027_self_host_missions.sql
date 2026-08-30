CREATE TABLE IF NOT EXISTS self_host_missions (
    id TEXT PRIMARY KEY,
    project_root TEXT NOT NULL,
    objective TEXT NOT NULL,
    status TEXT NOT NULL,
    current_task_id TEXT,
    base_revision TEXT NOT NULL,
    design_bundle_hash TEXT NOT NULL,
    gate_bundle_hash TEXT NOT NULL,
    candidate_fingerprint TEXT,
    plan TEXT NOT NULL DEFAULT '{}',
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (current_task_id) REFERENCES tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_self_host_missions_status
    ON self_host_missions(status, updated_at);
