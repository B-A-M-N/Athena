"""Durable-neutral models for dynamically constructed affordances.

These are records and contracts, not an alternate execution path.  Generated
source is always invoked by the ordinary capability dispatcher and therefore
cannot use its declared effect envelope as authority.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AffordanceScope(str, Enum):
    SCRATCH = "scratch"
    TASK = "task"
    CANDIDATE = "candidate"
    PROJECT = "project"
    USER = "user"
    SYSTEM = "system"


@dataclass(frozen=True)
class DependencyRequirement:
    """A dependency request that can be inspected/resolved by policy."""

    name: str
    manager: str = "python"
    version: str | None = None
    reason: str = ""
    required_for: str | None = None

    def key(self) -> str:
        return f"{self.manager}:{self.name}:{self.version or '*'}"


@dataclass(frozen=True)
class GeneratedCapability:
    """Description and proof envelope for generated executable machinery."""

    id: str
    name: str
    description: str
    implementation: str
    runtime: str = "python"
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    output_schema: Mapping[str, Any] | None = None
    required_capabilities: tuple[str, ...] = ()
    required_dependencies: tuple[DependencyRequirement, ...] = ()
    declared_effects: frozenset[str] = frozenset()
    effective_authority: frozenset[str] = frozenset()
    scope: AffordanceScope = AffordanceScope.TASK
    task_scope: str | None = None
    project_scope: str | None = None
    user_scope: str | None = None
    provenance: Mapping[str, Any] = field(default_factory=dict)
    code_hash: str = ""
    schema_hash: str = ""
    validation_state: str = "DRAFT"
    proof_record: Mapping[str, Any] = field(default_factory=dict)
    version: int = 1
    lifecycle_state: str = "DRAFT"
    supersedes: tuple[str, ...] = ()
    quality_score: float = 0.0
    use_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    last_used_at: str | None = None
    dependency_lock: Mapping[str, Any] = field(default_factory=dict)
    lifecycle_history: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        computed_code_hash = hashlib.sha256(
            self.implementation.encode()).hexdigest()
        if self.code_hash and self.code_hash != computed_code_hash:
            raise ValueError(
                "generated capability code_hash does not match implementation")
        if not self.code_hash:
            object.__setattr__(self, "code_hash", computed_code_hash)

        schema_value = {
            "input": dict(self.input_schema),
            "output": dict(self.output_schema or {}),
        }
        computed_schema_hash = hashlib.sha256(
            json.dumps(schema_value, sort_keys=True).encode()).hexdigest()
        if self.schema_hash and self.schema_hash != computed_schema_hash:
            raise ValueError(
                "generated capability schema_hash does not match schemas")
        if not self.schema_hash:
            object.__setattr__(self, "schema_hash", computed_schema_hash)

    @property
    def code(self) -> str:
        """Compatibility alias for synthesis engines that call it ``code``."""
        return self.implementation

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "implementation": self.implementation,
            "runtime": self.runtime,
            "input_schema": dict(self.input_schema),
            "output_schema": dict(self.output_schema or {}),
            "required_capabilities": list(self.required_capabilities),
            "required_dependencies": [d.__dict__ for d in self.required_dependencies],
            "declared_effects": sorted(self.declared_effects),
            "effective_authority": sorted(self.effective_authority),
            "scope": self.scope.value,
            "task_scope": self.task_scope,
            "project_scope": self.project_scope,
            "user_scope": self.user_scope,
            "provenance": dict(self.provenance),
            "code_hash": self.code_hash,
            "schema_hash": self.schema_hash,
            "validation_state": self.validation_state,
            "proof_record": dict(self.proof_record),
            "version": self.version,
            "lifecycle_state": self.lifecycle_state,
            "supersedes": list(self.supersedes),
            "quality_score": self.quality_score,
            "use_count": self.use_count,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "last_used_at": self.last_used_at,
            "dependency_lock": dict(self.dependency_lock),
            "lifecycle_history": [dict(event) for event in self.lifecycle_history],
        }

    @classmethod
    def from_record(cls, data: Mapping[str, Any]) -> GeneratedCapability:
        """Rehydrate a generated record after restart."""
        dependencies = tuple(
            DependencyRequirement(**dict(dependency))
            for dependency in data.get("required_dependencies") or ()
        )
        return cls(
            id=str(data["id"]),
            name=str(data.get("name") or data["id"]),
            description=str(data.get("description") or ""),
            implementation=str(data.get("implementation") or ""),
            runtime=str(data.get("runtime") or "python"),
            input_schema=dict(data.get("input_schema") or {}),
            output_schema=dict(data.get("output_schema") or {}) or None,
            required_capabilities=tuple(data.get("required_capabilities") or ()),
            required_dependencies=dependencies,
            declared_effects=frozenset(data.get("declared_effects") or ()),
            effective_authority=frozenset(data.get("effective_authority") or ()),
            scope=AffordanceScope(
                data.get("scope", AffordanceScope.TASK.value)),
            task_scope=data.get("task_scope"),
            project_scope=data.get("project_scope"),
            user_scope=data.get("user_scope"),
            provenance=dict(data.get("provenance") or {}),
            code_hash=str(data.get("code_hash") or ""),
            schema_hash=str(data.get("schema_hash") or ""),
            validation_state=str(data.get("validation_state") or "DRAFT"),
            proof_record=dict(data.get("proof_record") or {}),
            version=int(data.get("version") or 1),
            lifecycle_state=str(data.get("lifecycle_state") or "DRAFT"),
            supersedes=tuple(str(item) for item in data.get("supersedes") or ()),
            quality_score=float(data.get("quality_score") or 0.0),
            use_count=int(data.get("use_count") or 0),
            success_count=int(data.get("success_count") or 0),
            failure_count=int(data.get("failure_count") or 0),
            last_used_at=data.get("last_used_at"),
            dependency_lock=dict(data.get("dependency_lock") or {}),
            lifecycle_history=tuple(
                dict(event) for event in data.get("lifecycle_history") or ()
            ),
        )


@dataclass(frozen=True)
class ScratchProgram:
    """Cheap task-local computation; not automatically promoted."""

    id: str
    code: str
    runtime: str = "python"
    task_id: str | None = None
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    output_schema: Mapping[str, Any] | None = None
    effects: frozenset[str] = frozenset({"READ_LOCAL"})
    provenance: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScratchHelper:
    program: ScratchProgram
    purpose: str = ""


@dataclass(frozen=True)
class ScratchAdapter:
    program: ScratchProgram
    input_kind: str = "text"
    output_kind: str = "structured"


@dataclass(frozen=True)
class ScratchAnalyzer:
    program: ScratchProgram
    observation_kind: str = "analysis"


__all__ = [
    "AffordanceScope", "DependencyRequirement", "GeneratedCapability",
    "ScratchAdapter", "ScratchAnalyzer", "ScratchHelper", "ScratchProgram",
]
