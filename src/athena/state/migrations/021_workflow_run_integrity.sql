-- Immutable workflow-run identity and crash-idempotent step receipts.
-- 019 created these tables before restart safety was part of the contract;
-- keep this as an additive migration for existing databases.
ALTER TABLE workflow_runs ADD COLUMN definition_hash TEXT;
ALTER TABLE workflow_runs ADD COLUMN input_hash TEXT;
ALTER TABLE workflow_runs ADD COLUMN workspace_identity TEXT;
ALTER TABLE workflow_runs ADD COLUMN workspace_revision TEXT;
ALTER TABLE workflow_runs ADD COLUMN environment_identity TEXT;

ALTER TABLE workflow_step_runs ADD COLUMN execution_records TEXT NOT NULL DEFAULT '[]';
ALTER TABLE workflow_step_runs ADD COLUMN execution_id TEXT;
ALTER TABLE workflow_step_runs ADD COLUMN call_id TEXT;
ALTER TABLE workflow_step_runs ADD COLUMN argument_digest TEXT;
ALTER TABLE workflow_step_runs ADD COLUMN capability_id TEXT;
ALTER TABLE workflow_step_runs ADD COLUMN state TEXT NOT NULL DEFAULT 'PENDING';
ALTER TABLE workflow_step_runs ADD COLUMN approval_id TEXT;
ALTER TABLE workflow_step_runs ADD COLUMN continuation_id TEXT;

CREATE INDEX IF NOT EXISTS idx_workflow_step_call
    ON workflow_step_runs(run_id, call_id);
