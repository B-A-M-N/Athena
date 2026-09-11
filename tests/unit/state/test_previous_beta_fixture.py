"""Keep the clean-install N-1 fixture tied to the previous installed wheel."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[3]
_FIXTURE = _ROOT / "tests" / "fixtures" / "release" / "previous-beta-schema.sql"
_METADATA = _ROOT / "tests" / "fixtures" / "release" / "previous-beta-release.json"


def _schema(sql: str) -> list[tuple[str, str, str]]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(sql)
        return [
            (str(row[0]), str(row[1]), str(row[2] or ""))
            for row in connection.execute(
                "SELECT type, name, COALESCE(sql, '') FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        ]
    finally:
        connection.close()


def test_previous_beta_fixture_matches_the_previous_artifact_fingerprint() -> None:
    metadata = json.loads(_METADATA.read_text(encoding="utf-8"))
    objects = _schema(_FIXTURE.read_text(encoding="utf-8"))
    fingerprint = hashlib.sha256(
        json.dumps(objects, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    assert fingerprint == metadata["schema_fingerprint_sha256"]
    assert metadata["schema_migration_version"] == "038"
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(_FIXTURE.read_text(encoding="utf-8"))
        versions = [
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY CAST(version AS INTEGER)"
            )
        ]
        assert versions == [f"{value:03d}" for value in range(1, 39)]
        for table, key in (
            ("sessions", "previous-session"),
            ("tasks", "previous-task"),
            ("messages", "previous-message"),
            ("memories", "previous-memory"),
            ("scheduled_jobs", "previous-job"),
            ("approvals", "previous-approval"),
            ("mutations", "previous-mutation"),
            ("workflows", "previous-workflow"),
            ("workflow_runs", "previous-workflow-run"),
            ("capability_packs", "previous-pack"),
            ("provider_usage", "previous-usage"),
            ("self_host_missions", "previous-mission"),
            ("delegate_sessions", "previous-delegate"),
            ("continuations", "previous-continuation"),
        ):
            assert (
                connection.execute(f"SELECT 1 FROM {table} WHERE id = ?", (key,)).fetchone()
                is not None
            )
    finally:
        connection.close()


def test_previous_beta_fixture_has_reproducible_release_provenance() -> None:
    metadata = json.loads(_METADATA.read_text(encoding="utf-8"))
    migration_manifest = _FIXTURE.parent / str(metadata["migration_manifest"])

    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    assert metadata["schema_migration_version"] == "038"
    assert metadata["schema_fixture_sha256"] == digest(_FIXTURE)
    assert metadata["migration_manifest_sha256"] == digest(migration_manifest)
    assert metadata["source_sha"] == "5a402414de71c650aef9b18c002f19f206ae7f6a"
    assert "generate-previous-release-fixture" in metadata["generation_command"]
    assert len(metadata["previous_release_artifacts"]["python_wheel"]["sha256"]) == 64
    for artifact in metadata["previous_release_artifacts"].values():
        assert len(artifact["sha256"]) == 64
        assert artifact["name"].startswith("athena_agent-")
