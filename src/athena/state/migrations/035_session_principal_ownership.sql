ALTER TABLE sessions ADD COLUMN principal_id TEXT;
ALTER TABLE sessions ADD COLUMN project_id TEXT;

CREATE INDEX IF NOT EXISTS idx_sessions_principal
    ON sessions(principal_id, created_at);
CREATE INDEX IF NOT EXISTS idx_sessions_principal_project
    ON sessions(principal_id, project_id, created_at);
