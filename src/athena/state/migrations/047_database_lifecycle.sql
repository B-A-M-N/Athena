-- Startup diagnostics must distinguish a clean stop from an interrupted
-- process without attempting to repair application data.
CREATE TABLE IF NOT EXISTS database_lifecycle (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
