"""Validated Python dependency environments for generated capabilities.

Dependency installation and generated execution are separate operations.  This
module is the narrow boundary between them: it accepts only Athena's lock
format, verifies the installed distribution and RECORD hashes, and returns a
workspace-local import path that can be injected into a child interpreter.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import base64
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import unquote

from athena.protocol.affordances import DependencyRequirement
from athena.execution.dependency_lock import (
    calculate_environment_fingerprint,
    parse_dependency_lock,
    record_manifest as calculate_record_manifest,
    sha256_file,
)


class DependencyEnvironmentError(ValueError):
    """The requested dependency environment is absent or no longer valid."""


@dataclass(frozen=True)
class DependencyEnvironment:
    """A verified import environment for one workspace."""

    target: Path
    packages: tuple[Mapping[str, Any], ...]
    fingerprint: str

    @property
    def python_path(self) -> tuple[str, ...]:
        return (str(self.target),)

    def to_metadata(self) -> dict[str, Any]:
        return {
            "target": str(self.target),
            "packages": [dict(package) for package in self.packages],
            "environment_fingerprint": self.fingerprint,
        }


def resolve_dependency_environment(
    workspace_root: str | Path,
    requirements: Sequence[DependencyRequirement],
    *,
    expected_fingerprint: str | None = None,
) -> DependencyEnvironment:
    """Resolve and verify the locked Python packages for a workspace.

    The package target is deliberately derived from the workspace instead of
    trusting a path in the lock file.  A generated capability therefore cannot
    turn its dependency declaration into an arbitrary host import path.
    """
    root = Path(workspace_root).resolve()
    lock_path = root / ".athena" / "dependencies.lock.json"
    try:
        lock = parse_dependency_lock(lock_path.read_bytes())
    except (FileNotFoundError, OSError, TypeError, ValueError) as exc:
        raise DependencyEnvironmentError(
            f"dependency lock is missing or invalid: {lock_path}"
        ) from exc
    packages = lock.get("packages") if isinstance(lock, dict) else None
    if not isinstance(packages, dict):
        raise DependencyEnvironmentError("dependency lock has no package map")
    try:
        lock_format = int(lock.get("format") or 1)
    except (TypeError, ValueError) as exc:
        raise DependencyEnvironmentError("dependency lock format is invalid") from exc
    if lock_format not in {1, 2}:
        raise DependencyEnvironmentError(f"unsupported dependency lock format: {lock_format}")
    environment_id = str(lock.get("environment_id") or "")
    if not environment_id:
        for value in packages.values():
            if isinstance(value, Mapping) and value.get("environment_id"):
                environment_id = str(value["environment_id"])
                break
    target = dependency_environment_target(root, environment_id or None, "python")
    if not target.is_dir():
        raise DependencyEnvironmentError(f"dependency environment is missing: {target}")

    verified: list[Mapping[str, Any]] = []
    verified_names: set[str] = set()
    runtime_identity = _python_runtime_identity()
    for requirement in requirements:
        if requirement.manager != "python":
            raise DependencyEnvironmentError(
                f"unsupported generated dependency manager: {requirement.manager}"
            )
        record = _find_record(packages, requirement.name)
        if record is None:
            raise DependencyEnvironmentError(
                f"dependency {requirement.name!r} is not present in the workspace lock"
            )
        resolved_version = str(record.get("resolved_version") or "")
        if requirement.version and resolved_version != requirement.version:
            raise DependencyEnvironmentError(
                f"dependency {requirement.name!r} version mismatch: "
                f"required {requirement.version}, locked {resolved_version}"
            )
        closure = record.get("closure")
        locked_packages = closure if isinstance(closure, list) else [record]
        for index, locked in enumerate(locked_packages):
            if not isinstance(locked, Mapping):
                raise DependencyEnvironmentError(
                    "dependency lock contains an invalid closure entry"
                )
            package_name = str(locked.get("name") or (requirement.name if index == 0 else ""))
            normalized_name = _normalize(package_name)
            if not package_name or normalized_name in verified_names:
                continue
            distribution = _find_distribution(target, package_name)
            if distribution is None:
                raise DependencyEnvironmentError(
                    f"locked dependency {package_name!r} is not installed"
                )
            installed_version = str(distribution.version or "")
            locked_version = str(locked.get("resolved_version") or "")
            if installed_version != locked_version:
                raise DependencyEnvironmentError(
                    f"dependency {package_name!r} changed from locked version "
                    f"{locked_version} to {installed_version}"
                )
            hashes, record_entry_count, record_manifest_sha256 = record_manifest(distribution)
            verify_record_files(distribution)
            expected_runtime = locked.get("runtime_identity") or record.get("runtime_identity")
            if expected_runtime and expected_runtime != runtime_identity:
                raise DependencyEnvironmentError(
                    f"dependency {package_name!r} runtime identity changed"
                )
            expected_hashes = sorted(str(item) for item in locked.get("record_hashes") or ())
            if expected_hashes and hashes != expected_hashes:
                raise DependencyEnvironmentError(
                    f"dependency {package_name!r} RECORD hash mismatch"
                )
            expected_count = locked.get("record_entry_count")
            if expected_count is not None and int(expected_count) != record_entry_count:
                raise DependencyEnvironmentError(
                    f"dependency {package_name!r} RECORD entry count mismatch"
                )
            expected_manifest = str(locked.get("record_manifest_sha256") or "")
            if expected_manifest and expected_manifest != record_manifest_sha256:
                raise DependencyEnvironmentError(
                    f"dependency {package_name!r} RECORD manifest mismatch"
                )
            package: dict[str, Any] = {
                "name": package_name,
                "resolved_version": installed_version,
                "record_hashes": hashes,
            }
            if (
                locked.get("record_manifest_sha256")
                and locked.get("record_entry_count") is not None
            ):
                package["record_entry_count"] = record_entry_count
                package["record_manifest_sha256"] = record_manifest_sha256
            verified_names.add(normalized_name)
            verified.append(package)
        expected_package_fingerprint = record.get("environment_fingerprint")
        if expected_package_fingerprint:
            fingerprint_version = _fingerprint_version(lock, record, lock_format)
            closure_packages = [
                {
                    "name": str(item.get("name") or (requirement.name if index == 0 else "")),
                    "resolved_version": str(item.get("resolved_version") or ""),
                    "record_hashes": sorted(
                        str(value) for value in item.get("record_hashes") or ()
                    ),
                    **(
                        {
                            "record_entry_count": int(item["record_entry_count"]),
                            "record_manifest_sha256": str(item["record_manifest_sha256"]),
                        }
                        if item.get("record_manifest_sha256")
                        and item.get("record_entry_count") is not None
                        else {}
                    ),
                }
                for index, item in enumerate(locked_packages)
                if isinstance(item, Mapping)
            ]
            package_fingerprint = environment_fingerprint(
                closure_packages,
                runtime_identity=runtime_identity if fingerprint_version >= 2 else None,
            )
            if expected_package_fingerprint != package_fingerprint:
                raise DependencyEnvironmentError(
                    f"dependency {requirement.name!r} environment fingerprint mismatch"
                )

    fingerprint_version = max(
        (
            _fingerprint_version(lock, record, lock_format)
            for record in packages.values()
            if isinstance(record, Mapping)
        ),
        default=1,
    )
    fingerprint = environment_fingerprint(
        verified,
        runtime_identity=runtime_identity if fingerprint_version >= 2 else None,
    )
    if expected_fingerprint and fingerprint != expected_fingerprint:
        raise DependencyEnvironmentError(
            "dependency environment fingerprint does not match generated capability"
        )
    return DependencyEnvironment(
        target=target,
        packages=tuple(verified),
        fingerprint=fingerprint,
    )


def record_hashes(distribution: Any) -> list[str]:
    """Return the canonical hashed entries from a distribution RECORD file."""
    return record_manifest(distribution)[0]


def record_manifest(distribution: Any) -> tuple[list[str], int, str]:
    """Return a bounded preview plus a digest of every hashed RECORD entry."""
    return calculate_record_manifest(distribution.read_text("RECORD"))


def verify_record_files(distribution: Any) -> None:
    """Verify every hashed RECORD entry against the installed file bytes."""
    record_text = distribution.read_text("RECORD")
    locate_file = getattr(distribution, "locate_file", None)
    if not record_text or locate_file is None:
        raise DependencyEnvironmentError("installed dependency has no verifiable RECORD file")
    for line in record_text.splitlines():
        path, encoded, *_ = line.split(",", 2) + [""]
        if not encoded.startswith("sha256="):
            continue
        expected = encoded.removeprefix("sha256=")
        candidate = Path(locate_file(unquote(path))).resolve()
        if not candidate.is_file():
            raise DependencyEnvironmentError(f"dependency RECORD entry is missing: {path}")
        actual = (
            base64.urlsafe_b64encode(bytes.fromhex(sha256_file(candidate)))
            .rstrip(b"=")
            .decode("ascii")
        )
        if actual != expected:
            raise DependencyEnvironmentError(f"dependency RECORD content hash mismatch: {path}")


def environment_fingerprint(
    packages: Sequence[Mapping[str, Any]], *, runtime_identity: str | None = None
) -> str:
    return calculate_environment_fingerprint(packages, runtime_identity=runtime_identity)


def _fingerprint_version(
    lock: Mapping[str, Any], record: Mapping[str, Any], lock_format: int
) -> int:
    raw = record.get("fingerprint_version", lock.get("fingerprint_version"))
    if raw is None:
        return 2 if lock_format >= 2 else 1
    try:
        version = int(raw)
    except (TypeError, ValueError) as exc:
        raise DependencyEnvironmentError("dependency fingerprint version is invalid") from exc
    if version not in {1, 2}:
        raise DependencyEnvironmentError(f"unsupported dependency fingerprint version: {version}")
    return version


def _python_runtime_identity() -> str:
    """Return the exact interpreter identity used for dependency imports."""
    from athena.execution.environment import ProjectEnvironmentFingerprint

    return ProjectEnvironmentFingerprint._executable_identity("python", sys.executable)


python_runtime_identity = _python_runtime_identity


def _find_record(packages: Mapping[str, Any], name: str) -> Mapping[str, Any] | None:
    wanted = _normalize(name)
    for key, value in packages.items():
        if _normalize(str(key)) == wanted and isinstance(value, Mapping):
            return value
    return None


def _find_distribution(target: Path, name: str) -> Any | None:
    wanted = _normalize(name)
    for distribution in importlib.metadata.distributions(path=[str(target)]):
        dist_name = str(distribution.metadata.get("Name") or "")
        if _normalize(dist_name) == wanted:
            return distribution
    return None


def _normalize(value: str) -> str:
    return value.replace("-", "_").casefold()


def dependency_environment_id(lock: Mapping[str, Any]) -> str:
    """Return the stable content address for a dependency lock snapshot."""
    payload = json.dumps(lock, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def dependency_environment_target(
    workspace_root: str | Path,
    environment_id: str | None,
    manager: str,
) -> Path:
    """Resolve an isolated environment path from its lock content address.

    Legacy locks without an environment id remain readable for migration, but
    every new install/replay path is addressed under ``.athena/environments``.
    """
    root = Path(workspace_root).resolve()
    if environment_id:
        if not re.fullmatch(r"[0-9a-f]{64}", str(environment_id)):
            raise DependencyEnvironmentError("dependency environment id is not a SHA-256 digest")
        target = root / ".athena" / "environments" / str(environment_id) / str(manager)
    else:
        target = root / ".athena" / "dependencies"
    target = target.resolve()
    if root not in target.parents:
        raise DependencyEnvironmentError("dependency target escaped workspace")
    return target


__all__ = [
    "DependencyEnvironment",
    "DependencyEnvironmentError",
    "dependency_environment_id",
    "dependency_environment_target",
    "environment_fingerprint",
    "record_hashes",
    "record_manifest",
    "resolve_dependency_environment",
    "verify_record_files",
]
