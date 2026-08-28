-- Replay identity and post-mutation environment baselines for workflow runs.
ALTER TABLE workflow_runs ADD COLUMN initial_environment_identity TEXT;
ALTER TABLE workflow_runs ADD COLUMN parent_call_id TEXT;
ALTER TABLE workflow_runs ADD COLUMN parent_workflow_id TEXT;

CREATE INDEX IF NOT EXISTS idx_workflow_runs_parent_call
    ON workflow_runs(task_id, parent_call_id, workflow_id);
