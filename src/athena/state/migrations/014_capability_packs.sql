CREATE TABLE IF NOT EXISTS capability_packs (
    id TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    manifest TEXT NOT NULL,
    install_path TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    source_integrity TEXT NOT NULL,
    installed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_capability_packs_enabled
    ON capability_packs(enabled, id);

CREATE TABLE IF NOT EXISTS capability_pack_contributions (
    pack_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    contribution_id TEXT NOT NULL,
    PRIMARY KEY(pack_id, kind, contribution_id),
    FOREIGN KEY(pack_id) REFERENCES capability_packs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_capability_pack_contributions_pack
    ON capability_pack_contributions(pack_id, kind);
