-- Crash-recoverable accounting identity for one real provider call.
ALTER TABLE model_response_receipts ADD COLUMN attempt_id TEXT;
ALTER TABLE model_response_receipts ADD COLUMN reservation_amount TEXT;
ALTER TABLE model_response_receipts ADD COLUMN reservation_applied_at TEXT;
ALTER TABLE model_response_receipts ADD COLUMN actual_input_tokens INTEGER;
ALTER TABLE model_response_receipts ADD COLUMN actual_output_tokens INTEGER;
ALTER TABLE model_response_receipts ADD COLUMN actual_cost TEXT;
ALTER TABLE model_response_receipts ADD COLUMN provider_usage_id TEXT;
ALTER TABLE model_response_receipts ADD COLUMN response_committed_at TEXT;
ALTER TABLE model_response_receipts ADD COLUMN accounting_applied_at TEXT;
ALTER TABLE model_response_receipts ADD COLUMN assistant_appended_at TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_model_response_receipts_attempt
    ON model_response_receipts(attempt_id) WHERE attempt_id IS NOT NULL;
