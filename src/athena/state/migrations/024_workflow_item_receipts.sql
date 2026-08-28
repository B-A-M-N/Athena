-- Normalized per-item workflow receipts.  The aggregate workflow step cursor
-- remains in workflow_step_runs; this table is the indexed source for
-- continuation and nested-run lookup.
CREATE TABLE IF NOT EXISTS workflow_step_item_runs (
    run_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    item_index INTEGER NOT NULL,
    execution_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    capability_id TEXT,
    argument_digest TEXT,
    state TEXT NOT NULL,
    output TEXT,
    failures TEXT NOT NULL DEFAULT '[]',
    approval_id TEXT,
    continuation_id TEXT,
    nested_run_id TEXT,
    external_transaction_id TEXT,
    output_recorded INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    completed_at TEXT,
    PRIMARY KEY (run_id, step_id, item_index),
    FOREIGN KEY (run_id) REFERENCES workflow_runs(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_workflow_item_call
    ON workflow_step_item_runs(call_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_workflow_item_execution
    ON workflow_step_item_runs(execution_id);

CREATE INDEX IF NOT EXISTS idx_workflow_item_nested
    ON workflow_step_item_runs(nested_run_id);
