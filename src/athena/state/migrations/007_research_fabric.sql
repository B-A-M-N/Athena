-- Durable Evidence/Research Fabric records.  Content lives in ArtifactStore;
-- these tables retain source identity, evidence links, and open gaps.
CREATE TABLE IF NOT EXISTS research_sources (
    id TEXT PRIMARY KEY,
    canonical_uri TEXT NOT NULL,
    title TEXT NOT NULL,
    source_type TEXT NOT NULL,
    authority_class TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    published_at TEXT,
    content_hash TEXT,
    artifact_uri TEXT,
    task_id TEXT,
    project_id TEXT,
    metadata TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_research_source_version
    ON research_sources(canonical_uri, content_hash);

CREATE TABLE IF NOT EXISTS research_evidence (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    claim_id TEXT,
    extracted_claim TEXT NOT NULL,
    exact_supporting_excerpt TEXT NOT NULL,
    locator TEXT NOT NULL,
    evidence_type TEXT NOT NULL,
    authority_class TEXT NOT NULL,
    extraction_method TEXT NOT NULL,
    extraction_model TEXT,
    confidence REAL,
    task_id TEXT,
    created_at TEXT NOT NULL,
    metadata TEXT NOT NULL,
    FOREIGN KEY(source_id) REFERENCES research_sources(id)
);
CREATE INDEX IF NOT EXISTS idx_research_evidence_source
    ON research_evidence(source_id);
CREATE INDEX IF NOT EXISTS idx_research_evidence_claim
    ON research_evidence(claim_id);

CREATE TABLE IF NOT EXISTS research_evidence_links (
    evidence_id TEXT NOT NULL,
    related_evidence_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    PRIMARY KEY(evidence_id, related_evidence_id, relation),
    FOREIGN KEY(evidence_id) REFERENCES research_evidence(id),
    FOREIGN KEY(related_evidence_id) REFERENCES research_evidence(id)
);

CREATE TABLE IF NOT EXISTS research_claim_evidence (
    claim_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    task_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(claim_id, evidence_id),
    FOREIGN KEY(evidence_id) REFERENCES research_evidence(id)
);

CREATE TABLE IF NOT EXISTS research_gaps (
    id TEXT PRIMARY KEY,
    objective TEXT NOT NULL,
    question TEXT NOT NULL,
    kind TEXT NOT NULL,
    required INTEGER NOT NULL,
    status TEXT NOT NULL,
    task_id TEXT,
    evidence_ids TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    metadata TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_research_gaps_task_status
    ON research_gaps(task_id, status);
