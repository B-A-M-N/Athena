ALTER TABLE pack_hook_outbox ADD COLUMN claim_token TEXT;
ALTER TABLE pack_hook_outbox ADD COLUMN claimed_at TEXT;
ALTER TABLE pack_hook_outbox ADD COLUMN claim_expires_at TEXT;
CREATE INDEX IF NOT EXISTS idx_pack_hook_claim_lease
    ON pack_hook_outbox(status, claim_expires_at, next_attempt_at);
