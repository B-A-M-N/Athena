ALTER TABLE tasks ADD COLUMN claimed_by TEXT;
ALTER TABLE tasks ADD COLUMN claim_started_at TEXT;
ALTER TABLE tasks ADD COLUMN lease_expires_at TEXT;