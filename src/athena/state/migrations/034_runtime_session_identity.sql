ALTER TABLE runtime_sessions ADD COLUMN runtime TEXT;
ALTER TABLE runtime_sessions ADD COLUMN cwd TEXT;
ALTER TABLE runtime_sessions ADD COLUMN workspace_identity TEXT;
ALTER TABLE runtime_sessions ADD COLUMN network_policy TEXT;
ALTER TABLE runtime_sessions ADD COLUMN process_identity TEXT;
ALTER TABLE runtime_sessions ADD COLUMN environment_fingerprint TEXT;
ALTER TABLE runtime_sessions ADD COLUMN runtime_version TEXT;

CREATE INDEX IF NOT EXISTS idx_runtime_sessions_backend_runtime
    ON runtime_sessions(backend, runtime);
