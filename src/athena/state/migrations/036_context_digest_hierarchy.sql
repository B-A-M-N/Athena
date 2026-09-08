ALTER TABLE context_digests ADD COLUMN parent_digest_id TEXT;
ALTER TABLE context_digests ADD COLUMN source_digest_ids TEXT NOT NULL DEFAULT '[]';
ALTER TABLE context_digests ADD COLUMN range_start_message_id TEXT;
ALTER TABLE context_digests ADD COLUMN range_end_message_id TEXT;

CREATE INDEX IF NOT EXISTS idx_context_digests_parent
    ON context_digests(parent_digest_id);
