"""Declarative workflow records.

Workflow steps are data, never arbitrary Python callbacks.  This makes a
workflow persistable, inspectable, replayable, and subject to the same
capability/policy boundary as a directly requested call.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from athena.affordances.models import AffordanceScope
from athena.protocol.ids import new_id


@dataclass(frozen=True)
class WorkflowStep:
    id: str
    capability_id: str | None = None
    workflow_id: str | None = None
    arguments: Mapping[str, Any] = field(default_factory=dict)
    if_condition: str | None = None
    foreach: str | None = None
    max_iterations: int = 100
    continue_on_error: bool = False

    def __post_init__(self) -> None:
        if bool(self.capability_id) == bool(self.workflow_id):
            raise ValueError("workflow step must name exactly one capability or workflow")
        if self.max_iterations < 1 or self.max_iterations > 10_000:
            raise ValueError("workflow step max_iterations must be between 1 and 10000")

    @classmethod
    def from_record(cls, data: Mapping[str, Any], index: int = 0) -> WorkflowStep:
        return cls(
            id=str(data.get("id") or f"step_{index + 1}"),
            capability_id=(str(data["capability"]) if data.get("capability") else None),
            workflow_id=(str(data["workflow"]) if data.get("workflow") else None),
            arguments=dict(data.get("arguments") or {}),
            if_condition=(str(data["if"]) if data.get("if") is not None
                          else str(data["if_condition"])
                          if data.get("if_condition") is not None else None),
            foreach=(str(data["foreach"]) if data.get("foreach") is not None
                     else None),
            max_iterations=int(data.get("max_iterations") or 100),
            continue_on_error=bool(data.get("continue_on_error", False)),
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            **({"capability": self.capability_id} if self.capability_id else {}),
            **({"workflow": self.workflow_id} if self.workflow_id else {}),
            "arguments": dict(self.arguments),
            **({"if": self.if_condition} if self.if_condition is not None else {}),
            **({"foreach": self.foreach} if self.foreach is not None else {}),
            "max_iterations": self.max_iterations,
            "continue_on_error": self.continue_on_error,
        }


@dataclass(frozen=True)
class Workflow:
    id: str
    name: str
    description: str
    steps: tuple[WorkflowStep, ...]
    scope: AffordanceScope = AffordanceScope.TASK
    task_scope: str | None = None
    project_scope: str | None = None
    user_scope: str | None = None
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    output_schema: Mapping[str, Any] | None = None
    provenance: Mapping[str, Any] = field(default_factory=dict)
    version: int = 1
    enabled: bool = True

    @classmethod
    def create(
        cls, *, name: str, description: str, steps: tuple[WorkflowStep, ...],
        **kwargs: Any,
    ) -> Workflow:
        return cls(id=new_id("workflow"), name=name, description=description,
                   steps=steps, **kwargs)

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "description": self.description,
            "steps": [step.to_record() for step in self.steps],
            "scope": self.scope.value, "task_scope": self.task_scope,
            "project_scope": self.project_scope,
            "user_scope": self.user_scope,
            "input_schema": dict(self.input_schema),
            "output_schema": dict(self.output_schema or {}),
            "provenance": dict(self.provenance), "version": self.version,
            "enabled": self.enabled,
        }

    @classmethod
    def from_record(cls, data: Mapping[str, Any]) -> Workflow:
        raw_scope = data.get("scope", AffordanceScope.TASK.value)
        return cls(
            id=str(data["id"]), name=str(data.get("name") or data["id"]),
            description=str(data.get("description") or ""),
            steps=tuple(WorkflowStep.from_record(step, i)
                        for i, step in enumerate(data.get("steps") or ())),
            scope=AffordanceScope(raw_scope),
            task_scope=data.get("task_scope"),
            project_scope=data.get("project_scope"),
            user_scope=data.get("user_scope"),
            input_schema=dict(data.get("input_schema") or {}),
            output_schema=dict(data.get("output_schema") or {}) or None,
            provenance=dict(data.get("provenance") or {}),
            version=int(data.get("version") or 1),
            enabled=bool(data.get("enabled", True)),
        )


__all__ = ["Workflow", "WorkflowStep"]
