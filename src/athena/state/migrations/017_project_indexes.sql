CREATE TABLE IF NOT EXISTS project_indexes (
    root TEXT PRIMARY KEY,
    index_revision TEXT NOT NULL,
    definition TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_project_indexes_updated
    ON project_indexes(updated_at);
