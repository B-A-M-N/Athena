-- Durable model-tool compatibility receipts. Raw and canonical arguments are
-- retained together so replay never depends on a future repair policy.
CREATE TABLE IF NOT EXISTS tool_repairs (
    id TEXT PRIMARY KEY,
    call_id TEXT NOT NULL UNIQUE,
    task_id TEXT,
    capability_id TEXT NOT NULL,
    origin TEXT NOT NULL,
    outcome TEXT NOT NULL,
    schema_hash TEXT,
    repair_policy_version TEXT NOT NULL,
    provider_profile_id TEXT,
    model_id TEXT,
    original_shape_hash TEXT,
    canonical_shape_hash TEXT,
    original_arguments TEXT NOT NULL,
    canonical_arguments TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tool_repairs_task
    ON tool_repairs(task_id, created_at);
