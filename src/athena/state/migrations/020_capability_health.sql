CREATE TABLE IF NOT EXISTS capability_health (
    capability_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    total_calls INTEGER NOT NULL DEFAULT 0,
    successes INTEGER NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_failure TEXT,
    last_failure_at REAL,
    last_success_at REAL,
    opened_at REAL,
    cooldown_seconds REAL NOT NULL DEFAULT 30.0,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_capability_health_status
    ON capability_health(status, updated_at);
