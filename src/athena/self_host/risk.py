"""Deterministic risk classification for operator review."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import PurePosixPath


class SelfHostRiskClassifier:
    """Classify candidate paths without model discretion."""

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
            "native",
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
            if isinstance(resource, Mapping):
                path = str(resource.get("path") or resource.get("resource") or "")
                operation = str(resource.get("operation") or "").lower()
                deleted = deleted or operation in {"delete", "remove", "unlink"}
            else:
                path = str(resource)
            normalized = path.replace("\\", "/").lstrip("./")
            if not normalized:
                continue
            paths.append(normalized)
            parts = PurePosixPath(normalized).parts
            if (
                any(part in cls._HIGH_COMPONENTS for part in parts)
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
