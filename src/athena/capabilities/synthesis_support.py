"""Shared deterministic support for synthesis candidate construction."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence

from athena.affordances.models import DependencyRequirement

__all__ = ["merge_dependency_requirements", "merge_validation_cases"]


def _fixture_hash(case: Mapping[str, object]) -> str:
    """Hash fixture content while excluding live-evidence annotations."""
    comparable = {
        key: value
        for key, value in case.items()
        if key
        not in {
            "id",
            "source",
            "failure_class",
            "observed_failure",
            "resolved_by_revision",
            "capability_family",
            "revision_first_seen",
            "environment_fingerprint",
            "expected_contract",
            "args",
            "input",
        }
    }
    if "input" in case:
        comparable["args"] = case["input"]
    elif "args" in case:
        comparable["args"] = case["args"]
    return hashlib.sha256(
        json.dumps(comparable, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def merge_validation_cases(*groups: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    """Keep the first copy of each deterministic fixture in corpus order."""
    merged: list[dict[str, object]] = []
    seen: set[str] = set()
    for group in groups:
        for raw_case in group:
            case = dict(raw_case)
            fingerprint = _fixture_hash(case)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            merged.append(case)
    return merged


def merge_dependency_requirements(
    *groups: Sequence[DependencyRequirement],
) -> tuple[DependencyRequirement, ...]:
    """Merge dependency requirements by their stable key."""
    merged: dict[str, DependencyRequirement] = {}
    for group in groups:
        for dependency in group:
            merged[dependency.key()] = dependency
    return tuple(merged[key] for key in sorted(merged))
