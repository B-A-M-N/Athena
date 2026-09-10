-- Crash-idempotent provider responses.  A request row is created before the
-- provider call and completed before the kernel advances to the next prompt.
-- The request fingerprint is task-scoped so a retry cannot reuse a response
-- from another task or a changed prompt.
CREATE TABLE IF NOT EXISTS model_response_receipts (
    task_id TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    request_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL,
    response TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    PRIMARY KEY (task_id, request_fingerprint),
    UNIQUE (task_id, request_id),
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_model_response_receipts_task
    ON model_response_receipts(task_id, created_at);
