-- Inactive workflow-learning evidence.  These rows are not workflow
-- definitions and are never exposed as model-visible capabilities.  They are
-- retained only long enough to establish repeatability across restarts.
CREATE TABLE IF NOT EXISTS workflow_observations (
    id TEXT PRIMARY KEY,
    trace_signature TEXT NOT NULL,
    task_id TEXT NOT NULL,
    workspace_id TEXT,
    workspace_revision TEXT,
    steps TEXT NOT NULL,
    argument_shape TEXT NOT NULL,
    verification TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (trace_signature, task_id)
);
CREATE INDEX IF NOT EXISTS idx_workflow_observations_signature
    ON workflow_observations(trace_signature, observed_at, created_at);
CREATE INDEX IF NOT EXISTS idx_workflow_observations_observed_at
    ON workflow_observations(observed_at);
