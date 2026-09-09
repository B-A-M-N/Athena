from __future__ import annotations

import json
from pathlib import Path

from athena.execution.dependency_lock import (
    parse_dependency_lock,
    read_dependency_lock,
    record_manifest,
    upgrade_v1_to_v2,
)


def test_golden_lock_formats_parse_and_v1_upgrade():
    fixture_root = Path(__file__).parents[2] / "fixtures" / "dependencies"
    v1 = read_dependency_lock(fixture_root / "lock-v1.json")
    v2 = read_dependency_lock(fixture_root / "lock-v2.json")

    assert v1["format"] == 1
    assert v2["format"] == 2
    upgraded = upgrade_v1_to_v2(v1)
    assert upgraded["format"] == 2
    assert upgraded["fingerprint_version"] == 2
    assert upgraded["packages"]["demo"]["fingerprint_version"] == 1
    assert parse_dependency_lock(json.dumps(v2)) == v2


def test_record_manifest_commits_to_all_entries_beyond_preview():
    record = "\n".join(f"package/file-{index}.py,sha256=hash-{index}," for index in range(10_001))
    preview, count, digest = record_manifest(record)
    changed = record.replace("hash-10000", "hash-modified")
    _, changed_count, changed_digest = record_manifest(changed)

    assert len(preview) == 10_000
    assert count == changed_count == 10_001
    assert digest != changed_digest
