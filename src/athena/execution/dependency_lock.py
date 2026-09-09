"""Canonical dependency-lock parsing and manifest primitives.

Writers, local verifiers, and remote supervisors all use the same bounded
representation: a preview for operator inspection plus a digest over every
canonical RECORD path/hash pair.  The preview is never the integrity claim.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

RECORD_PREVIEW_LIMIT = 10_000
SUPPORTED_LOCK_FORMATS = frozenset({1, 2})
SUPPORTED_FINGERPRINT_VERSIONS = frozenset({1, 2})


def canonical_record_entries(record_text: str) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for line in str(record_text or "").splitlines():
        parts = line.split(",", 2)
        if len(parts) >= 2 and parts[1].startswith("sha256="):
            entries.append((parts[0], parts[1]))
    entries.sort()
    return entries


def record_manifest(record_text: str) -> tuple[list[str], int, str]:
    """Return a bounded preview, complete entry count, and complete digest."""
    entries = canonical_record_entries(record_text)
    digest = hashlib.sha256()
    for path, record_hash in entries:
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(record_hash.encode("ascii"))
        digest.update(b"\n")
    return (
        [f"{path}:{record_hash}" for path, record_hash in entries[:RECORD_PREVIEW_LIMIT]],
        len(entries),
        digest.hexdigest(),
    )


def sha256_file(path: str | Path) -> str:
    """Hash a file in bounded chunks instead of loading it into memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def calculate_environment_fingerprint(
    packages: Sequence[Mapping[str, Any]], *, runtime_identity: str | None = None
) -> str:
    canonical_packages: list[dict[str, Any]] = []
    for package in packages:
        canonical: dict[str, Any] = {
            "name": str(package.get("name") or ""),
            "resolved_version": str(package.get("resolved_version") or ""),
        }
        if package.get("record_manifest_sha256") and package.get("record_entry_count") is not None:
            canonical["record_entry_count"] = int(package["record_entry_count"])
            canonical["record_manifest_sha256"] = str(package["record_manifest_sha256"])
        else:
            canonical["record_hashes"] = sorted(
                str(value) for value in package.get("record_hashes") or ()
            )
        canonical_packages.append(canonical)
    payload: Any = canonical_packages
    if runtime_identity:
        payload = {"packages": payload, "runtime_identity": runtime_identity}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def validate_dependency_lock(lock: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and copy the lock envelope without interpreting package paths."""
    value = dict(lock)
    try:
        lock_format = int(value.get("format") or 1)
    except (TypeError, ValueError) as exc:
        raise ValueError("dependency lock format is invalid") from exc
    if lock_format not in SUPPORTED_LOCK_FORMATS:
        raise ValueError(f"unsupported dependency lock format: {lock_format}")
    fingerprint_version = value.get("fingerprint_version")
    if fingerprint_version is not None:
        try:
            parsed = int(fingerprint_version)
        except (TypeError, ValueError) as exc:
            raise ValueError("dependency fingerprint version is invalid") from exc
        if parsed not in SUPPORTED_FINGERPRINT_VERSIONS:
            raise ValueError(f"unsupported dependency fingerprint version: {fingerprint_version}")
    packages = value.get("packages")
    if packages is not None and not isinstance(packages, Mapping):
        raise ValueError("dependency lock packages must be an object")
    return value


def parse_dependency_lock(value: str | bytes | Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return validate_dependency_lock(value)
    try:
        decoded = json.loads(value.decode("utf-8") if isinstance(value, bytes) else value)
    except (UnicodeDecodeError, TypeError, ValueError) as exc:
        raise ValueError("dependency lock is invalid JSON") from exc
    if not isinstance(decoded, Mapping):
        raise ValueError("dependency lock must be an object")
    return validate_dependency_lock(decoded)


def read_dependency_lock(path: str | Path) -> dict[str, Any]:
    try:
        return parse_dependency_lock(Path(path).read_bytes())
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ValueError("dependency lock could not be read") from exc


def upgrade_v1_to_v2(lock: Mapping[str, Any]) -> dict[str, Any]:
    """Upgrade the envelope while preserving legacy per-package semantics."""
    value = validate_dependency_lock(lock)
    if int(value.get("format") or 1) >= 2:
        return value
    upgraded = dict(value)
    upgraded["format"] = 2
    upgraded["fingerprint_version"] = 2
    packages = upgraded.get("packages")
    if isinstance(packages, Mapping):
        upgraded["packages"] = {
            str(name): (
                {**record, "fingerprint_version": 1} if isinstance(record, Mapping) else record
            )
            for name, record in packages.items()
        }
    return upgraded


__all__ = [
    "RECORD_PREVIEW_LIMIT",
    "calculate_environment_fingerprint",
    "canonical_record_entries",
    "parse_dependency_lock",
    "read_dependency_lock",
    "record_manifest",
    "sha256_file",
    "upgrade_v1_to_v2",
    "validate_dependency_lock",
]
