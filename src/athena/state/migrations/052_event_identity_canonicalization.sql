-- Canonicalize the stable event identity for compatibility compatibility.
-- A stable event ID is an idempotency key, not evidence by itself. Replay
-- must compare semantic identity while retaining legacy timestamp/version
-- as decode-only compatibility fields.
ALTER TABLE events ADD COLUMN identity_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE events ADD COLUMN identity_hash TEXT;
ALTER TABLE events ADD COLUMN identity_fields TEXT;
CREATE INDEX IF NOT EXISTS idx_events_identity_hash ON events(identity_hash);
