-- Retrying an unknown provider attempt is an authorization action, not an
-- outcome reconciliation. Keep the original financial liability open until
-- its outcome is confirmed or an operator explicitly closes that liability.
ALTER TABLE model_response_receipts ADD COLUMN retry_authorized_at TEXT;
ALTER TABLE model_response_receipts ADD COLUMN retry_authorized_by TEXT;
ALTER TABLE model_response_receipts ADD COLUMN replacement_attempt_id TEXT;

ALTER TABLE model_response_attempts ADD COLUMN retry_authorized_at TEXT;
ALTER TABLE model_response_attempts ADD COLUMN retry_authorized_by TEXT;
ALTER TABLE model_response_attempts ADD COLUMN replacement_attempt_id TEXT;

CREATE INDEX IF NOT EXISTS idx_model_response_attempts_retry_authorized
    ON model_response_attempts(task_id, retry_authorized_at, created_at);
