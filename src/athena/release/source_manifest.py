"""Canonical source-tree manifests for frozen release qualification."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


# These paths are produced by release/build/test tooling rather than being
# source inputs. They must not make a frozen checkout fail its own verification
# after a lane writes ordinary evidence or compiler caches.
EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        ".venv",
        ".artifacts",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "__pycache__",
        "build",
        "dist",
        "release-artifacts",
        "release-evidence",
        "target",
        ".tox",
        ".nox",
    }
)
EXCLUDED_FILES = frozenset(
    {
        ".coverage",
        "backend-passport.json",
        "endurance-receipt.json",
        "release-lane-results.json",
        "release-scenarios.json",
        "release-support-matrix.json",
        "toolchain-passport.json",
    }
)


def _included(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    if any(
        part in EXCLUDED_DIRECTORIES or part.endswith(".egg-info") for part in relative.parts[:-1]
    ):
        return False
    if path.name in EXCLUDED_FILES or path.name.startswith(".coverage."):
        return False
    return path.is_file()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_files(root: Path) -> list[dict[str, Any]]:
    """Return sorted path/hash/size records for the source inputs."""
    root = root.resolve()
    records = []
    for path in root.rglob("*"):
        if not _included(path, root):
            continue
        records.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": _sha256(path),
                "size": path.stat().st_size,
            }
        )
    return sorted(records, key=lambda item: str(item["path"]))


def tree_digest(files: list[dict[str, Any]]) -> str:
    payload = json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_manifest(root: Path, *, source_sha: str | None = None) -> dict[str, Any]:
    files = source_files(root)
    return {
        "kind": "athena_frozen_source_manifest",
        "schema_version": 1,
        "source_sha": source_sha,
        "files": files,
        "tree_sha256": tree_digest(files),
    }


def verify_manifest(
    root: Path,
    expected: Mapping[str, Any],
    *,
    source_sha: str | None = None,
) -> tuple[bool, str]:
    """Compare current source inputs against a previously archived manifest."""
    if expected.get("kind") != "athena_frozen_source_manifest":
        return False, "manifest kind is invalid"
    if int(expected.get("schema_version", 0)) != 1:
        return False, "manifest schema version is unsupported"
    expected_sha = expected.get("source_sha")
    if source_sha and expected_sha and str(expected_sha) != str(source_sha):
        return False, f"manifest source SHA is {expected_sha!r}, expected {source_sha!r}"
    expected_files = expected.get("files")
    if not isinstance(expected_files, list):
        return False, "manifest files are missing"
    actual_files = source_files(root)
    if actual_files != expected_files:
        expected_by_path = {
            str(item.get("path")): item for item in expected_files if isinstance(item, Mapping)
        }
        actual_by_path = {str(item.get("path")): item for item in actual_files}
        missing = sorted(set(expected_by_path) - set(actual_by_path))
        added = sorted(set(actual_by_path) - set(expected_by_path))
        changed = sorted(
            path
            for path in set(expected_by_path) & set(actual_by_path)
            if expected_by_path[path] != actual_by_path[path]
        )
        detail = []
        if missing:
            detail.append("missing=" + ",".join(missing[:8]))
        if added:
            detail.append("added=" + ",".join(added[:8]))
        if changed:
            detail.append("changed=" + ",".join(changed[:8]))
        return False, "source tree changed (" + "; ".join(detail) + ")"
    actual_digest = tree_digest(actual_files)
    if str(expected.get("tree_sha256") or "") != actual_digest:
        return False, "source tree digest does not match its canonical file manifest"
    return True, actual_digest


__all__ = ["build_manifest", "source_files", "tree_digest", "verify_manifest"]
