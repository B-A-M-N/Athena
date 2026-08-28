CREATE TABLE IF NOT EXISTS external_effect_receipts (
    transaction_id TEXT PRIMARY KEY,
    receipt_id TEXT NOT NULL,
    task_id TEXT,
    capability_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    status TEXT NOT NULL,
    external_identity TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    idempotency_key TEXT,
    response TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_external_effect_idempotency
    ON external_effect_receipts(idempotency_key);

CREATE UNIQUE INDEX IF NOT EXISTS uq_external_effect_identity_key
    ON external_effect_receipts(capability_id, external_identity, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
