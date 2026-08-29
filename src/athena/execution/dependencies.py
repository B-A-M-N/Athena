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
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import unquote

from athena.affordances.models import DependencyRequirement


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
    target = (root / ".athena" / "dependencies").resolve()
    if root not in target.parents:
        raise DependencyEnvironmentError("dependency target escaped workspace")
    if not target.is_dir():
        raise DependencyEnvironmentError(f"dependency environment is missing: {target}")

    lock_path = root / ".athena" / "dependencies.lock.json"
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, TypeError, ValueError) as exc:
        raise DependencyEnvironmentError(
            f"dependency lock is missing or invalid: {lock_path}"
        ) from exc
    packages = lock.get("packages") if isinstance(lock, dict) else None
    if not isinstance(packages, dict):
        raise DependencyEnvironmentError("dependency lock has no package map")

    verified: list[Mapping[str, Any]] = []
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
        distribution = _find_distribution(target, requirement.name)
        if distribution is None:
            raise DependencyEnvironmentError(
                f"locked dependency {requirement.name!r} is not installed"
            )
        installed_version = str(distribution.version or "")
        if installed_version != resolved_version:
            raise DependencyEnvironmentError(
                f"dependency {requirement.name!r} changed from locked version "
                f"{resolved_version} to {installed_version}"
            )
        hashes = record_hashes(distribution)
        verify_record_files(distribution)
        expected_runtime = record.get("runtime_identity")
        runtime_identity = _python_runtime_identity()
        if expected_runtime and expected_runtime != runtime_identity:
            raise DependencyEnvironmentError(
                f"dependency {requirement.name!r} runtime identity changed"
            )
        expected_hashes = sorted(str(item) for item in record.get("record_hashes") or ())
        if expected_hashes and hashes != expected_hashes:
            raise DependencyEnvironmentError(
                f"dependency {requirement.name!r} RECORD hash mismatch"
            )
        package = {
            "name": requirement.name,
            "resolved_version": installed_version,
            "record_hashes": hashes,
        }
        package_fingerprint = environment_fingerprint(
            (package,), runtime_identity=runtime_identity if expected_runtime else None
        )
        expected_package_fingerprint = record.get("environment_fingerprint")
        if expected_package_fingerprint and expected_package_fingerprint != package_fingerprint:
            raise DependencyEnvironmentError(
                f"dependency {requirement.name!r} environment fingerprint mismatch"
            )
        verified.append(package)

    runtime_identity = _python_runtime_identity()
    fingerprint = environment_fingerprint(
        verified,
        runtime_identity=runtime_identity
        if any(
            record.get("runtime_identity")
            for record in packages.values()
            if isinstance(record, Mapping)
        )
        else None,
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
    record_text = distribution.read_text("RECORD")
    hashes: list[str] = []
    if record_text:
        for line in record_text.splitlines():
            parts = line.split(",", 2)
            if len(parts) >= 2 and parts[1].startswith("sha256="):
                hashes.append(f"{parts[0]}:{parts[1]}")
    hashes.sort()
    return hashes[:10_000]


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
            base64.urlsafe_b64encode(hashlib.sha256(candidate.read_bytes()).digest())
            .rstrip(b"=")
            .decode("ascii")
        )
        if actual != expected:
            raise DependencyEnvironmentError(f"dependency RECORD content hash mismatch: {path}")


def environment_fingerprint(
    packages: Sequence[Mapping[str, Any]], *, runtime_identity: str | None = None
) -> str:
    payload: Any = list(packages)
    if runtime_identity:
        payload = {"packages": payload, "runtime_identity": runtime_identity}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _python_runtime_identity() -> str:
    """Return the exact interpreter identity used for dependency imports."""
    from athena.execution.environment import ProjectEnvironmentFingerprint

    return ProjectEnvironmentFingerprint._executable_identity("python", sys.executable)


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


__all__ = [
    "DependencyEnvironment",
    "DependencyEnvironmentError",
    "environment_fingerprint",
    "record_hashes",
    "resolve_dependency_environment",
    "verify_record_files",
]
