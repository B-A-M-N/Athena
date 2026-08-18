ALTER TABLE mutations ADD COLUMN status TEXT NOT NULL DEFAULT 'COMPLETED';
ALTER TABLE mutations ADD COLUMN before_ref TEXT;
ALTER TABLE mutations ADD COLUMN inverse TEXT;
CREATE INDEX idx_mutations_status ON mutations(status);