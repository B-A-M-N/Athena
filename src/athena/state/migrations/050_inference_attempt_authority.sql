-- Make one provider attempt the authority for mutable inference obligations.
-- The logical receipt remains the replay index; paired projection updates are
-- still retained for backward-compatible reads and are committed atomically.
ALTER TABLE model_response_attempts ADD COLUMN actual_input_tokens INTEGER;
ALTER TABLE model_response_attempts ADD COLUMN actual_output_tokens INTEGER;
ALTER TABLE model_response_attempts ADD COLUMN actual_cost TEXT;
ALTER TABLE model_response_attempts ADD COLUMN response_committed_at TEXT;
ALTER TABLE model_response_attempts ADD COLUMN accounting_applied_at TEXT;
ALTER TABLE model_response_attempts ADD COLUMN idempotency_key TEXT;
ALTER TABLE model_response_attempts ADD COLUMN idempotency_semantics TEXT NOT NULL DEFAULT 'none';
ALTER TABLE model_response_attempts ADD COLUMN provider_outcome_note TEXT;
ALTER TABLE model_response_attempts ADD COLUMN provider_outcome_resolved_at TEXT;

CREATE INDEX IF NOT EXISTS idx_model_response_attempts_unknown
    ON model_response_attempts(task_id, provider_outcome_status, created_at);
