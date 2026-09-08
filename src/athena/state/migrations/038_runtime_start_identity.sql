ALTER TABLE runtime_sessions ADD COLUMN start_identity TEXT;
CREATE INDEX IF NOT EXISTS idx_runtime_sessions_process_identity
    ON runtime_sessions(process_identity, start_identity);
