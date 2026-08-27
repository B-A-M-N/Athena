-- Durable definitions for the programmable Affordance Fabric.
CREATE TABLE IF NOT EXISTS workflows (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    scope TEXT NOT NULL,
    task_scope TEXT,
    project_scope TEXT,
    user_scope TEXT,
    definition TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workflows_scope
    ON workflows(scope, task_scope, project_scope, user_scope);

CREATE TABLE IF NOT EXISTS generated_capabilities (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    owner TEXT NOT NULL,
    project_scope TEXT,
    user_scope TEXT,
    definition TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_generated_scope_owner
    ON generated_capabilities(scope, owner);
