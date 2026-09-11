-- Keep each provider attempt's obligations independently replayable.  The
-- logical request row remains a fast replay index; this append-only table
-- preserves every failed attempt and its reservation lifecycle.
ALTER TABLE model_response_receipts ADD COLUMN reservation_released_at TEXT;
ALTER TABLE model_response_receipts ADD COLUMN budget_accounted_at TEXT;
ALTER TABLE model_response_receipts ADD COLUMN provider_usage_started_at TEXT;
ALTER TABLE model_response_receipts ADD COLUMN provider_usage_completed_at TEXT;
ALTER TABLE model_response_receipts ADD COLUMN provider_outcome_status TEXT;
ALTER TABLE model_response_receipts ADD COLUMN provider_response_id TEXT;
ALTER TABLE model_response_receipts ADD COLUMN idempotency_key TEXT;
ALTER TABLE model_response_receipts ADD COLUMN provider_outcome_unknown_at TEXT;

CREATE TABLE IF NOT EXISTS model_response_attempts (
    attempt_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    request_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL,
    reservation_amount TEXT,
    reservation_applied_at TEXT,
    reservation_released_at TEXT,
    budget_accounted_at TEXT,
    provider_usage_id TEXT,
    provider_usage_started_at TEXT,
    provider_usage_completed_at TEXT,
    provider_response_id TEXT,
    provider_outcome_status TEXT,
    provider_outcome_unknown_at TEXT,
    assistant_appended_at TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_model_response_attempts_request
    ON model_response_attempts(task_id, request_fingerprint, created_at);
