-- Explicitly attached, versioned context blocks.
CREATE TABLE IF NOT EXISTS context_blocks (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    content TEXT NOT NULL,
    scope TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    trust TEXT NOT NULL,
    max_tokens INTEGER NOT NULL,
    attached INTEGER NOT NULL,
    version INTEGER NOT NULL,
    provenance TEXT NOT NULL,
    metadata TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_context_blocks_scope
    ON context_blocks(scope, scope_id, attached, updated_at);
CREATE TABLE IF NOT EXISTS context_block_versions (
    block_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    label TEXT NOT NULL,
    content TEXT NOT NULL,
    scope TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    trust TEXT NOT NULL,
    max_tokens INTEGER NOT NULL,
    attached INTEGER NOT NULL,
    provenance TEXT NOT NULL,
    metadata TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(block_id, version)
);
CREATE INDEX IF NOT EXISTS idx_context_block_versions_block
    ON context_block_versions(block_id, version);
