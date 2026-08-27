-- Derived lexical index over captured research snapshots. The ArtifactStore
-- blob remains authoritative; this projection exists for local retrieval.
CREATE TABLE IF NOT EXISTS research_source_content (
    source_id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    mime_type TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(source_id) REFERENCES research_sources(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_research_source_content_hash
    ON research_source_content(content_hash);
