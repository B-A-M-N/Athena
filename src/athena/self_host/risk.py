"""Deterministic risk classification for operator review."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import PurePosixPath


class SelfHostRiskClassifier:
    """Classify candidate paths without model discretion."""

    _HIGH_PREFIXES = (
        "src/athena/self_host/",
        "src/athena/release/",
        "src/athena/kernel/",
        "src/athena/policy/",
        "src/athena/reality/",
        "src/athena/shadow/",
        "src/athena/recovery/",
        "src/athena/execution/",
        "src/athena/tasks/",
        "src/athena/state/",
        "src/athena/protocol/",
        ".github/workflows/",
        "tests/contract/",
        "tests/security/",
        "tests/crash/",
    )

    _HIGH_COMPONENTS = frozenset(
        {
            "capabilities",
            "execution",
            "kernel",
            "policy",
            "dispatcher.py",
            "tasks",
            "state",
            "recovery",
            "reality",
            "protocol",
            "verification",
            "release",
            "security",
            "service",
            "shadow",
            "self_host",
            "native",
            "contract",
            "security",
            "crash",
            "workflows",
            "scripts/release-check",
            "scripts/architecture-lint",
            "SELF_HOSTING.md",
            "SECURITY.md",
            "docs/ARCHITECTURE.md",
            "BUILDSPEC.md",
            "BEHAVIORSPEC.md",
            "SPEC.md",
        }
    )
    _MEDIUM_COMPONENTS = frozenset(
        {"tests", "pyproject.toml", "uv.lock", "Cargo.toml", "Cargo.lock"}
    )

    @classmethod
    def classify(cls, changed_resources: Iterable[Mapping[str, object] | str]) -> dict[str, object]:
        paths: list[str] = []
        deleted = False
        reasons: set[str] = set()
        level = "low"
        for resource in changed_resources:
            operation = ""
            if isinstance(resource, Mapping):
                path = str(resource.get("path") or resource.get("resource") or "")
                operation = str(resource.get("operation") or "").lower()
            else:
                path = str(resource)
            normalized = path.replace("\\", "/").lstrip("./")
            if not normalized:
                continue
            paths.append(normalized)
            parts = PurePosixPath(normalized).parts
            before = resource.get("before_hash") if isinstance(resource, Mapping) else None
            after = resource.get("after_hash") if isinstance(resource, Mapping) else None
            if operation not in {"delete", "remove", "unlink"}:
                if before is not None and after is None:
                    operation = "delete"
                elif before is None and after is not None:
                    operation = "add"
                else:
                    operation = "modify"
            deleted = deleted or operation in {"delete", "remove", "unlink"}
            if (
                normalized.startswith(cls._HIGH_PREFIXES)
                or any(part in cls._HIGH_COMPONENTS for part in parts)
                or normalized in cls._HIGH_COMPONENTS
            ):
                level = "high"
                reasons.add("protected runtime or authority surface")
            elif level != "high" and (
                any(part in cls._MEDIUM_COMPONENTS for part in parts)
                or normalized in cls._MEDIUM_COMPONENTS
            ):
                level = "medium"
                reasons.add("build, dependency, or test surface")
        if deleted:
            level = "high"
            reasons.add("candidate deletes a resource")
        if not paths:
            reasons.add("candidate has no changed resources")
        return {
            "level": level,
            "paths": sorted(set(paths)),
            "reasons": sorted(reasons),
            "requires_operator_promotion": True,
            "requires_independent_review": level == "high",
        }


__all__ = ["SelfHostRiskClassifier"]
