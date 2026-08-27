"""Static validation for declarative workflow graphs."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from athena.capabilities.registry import validate_schema
from athena.workflows.models import Workflow


@dataclass(frozen=True)
class WorkflowValidation:
    ok: bool
    errors: tuple[str, ...] = ()
    effects: tuple[str, ...] = ()


class WorkflowValidator:
    """Validate references and calculate a conservative effect envelope."""

    def __init__(self, resolver: Callable[[str], Any] | None = None) -> None:
        self._resolver = resolver

    def validate(self, workflow: Workflow) -> WorkflowValidation:
        errors: list[str] = []
        effects: set[str] = set()

        # Validate the workflow's own model-facing input contract before any
        # step can run. Generated workflows may infer this schema from their
        # construction fixtures, while no-input workflows can leave it empty.
        try:
            validate_schema(dict(workflow.input_schema), {})
        except (TypeError, ValueError) as exc:
            errors.append(f"workflow input_schema: {exc}")

        def walk(current: Workflow, path: tuple[str, ...]) -> None:
            if current.id in path:
                errors.append("workflow cycle: " + " -> ".join((*path, current.id)))
                return
            if not current.enabled:
                errors.append(f"workflow {current.id} is disabled")
                return
            for step in current.steps:
                if step.if_condition is not None:
                    condition = step.if_condition.strip()
                    if not condition or condition.count("==") + condition.count("!=") > 1:
                        errors.append(f"{step.id}: invalid declarative condition")
                if step.foreach is not None and not step.foreach.startswith("$"):
                    errors.append(f"{step.id}: foreach must be a reference")
                if step.capability_id:
                    if self._resolver is None:
                        continue
                    try:
                        descriptor = self._resolver(step.capability_id)
                    except Exception as exc:  # noqa: BLE001 - resolver boundary
                        errors.append(f"{step.id}: unknown capability {step.capability_id}: {exc}")
                        continue
                    # Resolve literal operation arguments against the
                    # capability-owned effect contract. References are
                    # intentionally conservative until runtime resolution.
                    try:
                        step_effects = _step_effects(descriptor, step.arguments)
                    except ValueError as exc:
                        errors.append(f"{step.id}: {exc}")
                        continue
                    effects.update(effect.value for effect in step_effects)
                elif step.workflow_id:
                    if self._resolver is None:
                        continue
                    try:
                        nested = self._resolver(step.workflow_id)
                    except Exception as exc:  # noqa: BLE001 - resolver boundary
                        errors.append(f"{step.id}: unknown workflow {step.workflow_id}: {exc}")
                        continue
                    if not isinstance(nested, Workflow):
                        errors.append(f"{step.id}: {step.workflow_id} is not a workflow")
                    else:
                        walk(nested, (*path, current.id))

        walk(workflow, ())
        return WorkflowValidation(ok=not errors, errors=tuple(errors),
                                  effects=tuple(sorted(effects)))


def _step_effects(descriptor: Any, arguments: Mapping[str, Any]) -> frozenset[Any]:
    """Return exact effects for static arguments or a safe envelope."""
    if any(_contains_reference(value) for value in arguments.values()):
        return frozenset(descriptor.effects)
    try:
        resolved = descriptor.resolve_effects(arguments)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"operation effects: {exc}") from exc
    return frozenset(resolved or descriptor.effects)


def _contains_reference(value: Any) -> bool:
    if isinstance(value, str):
        return value.startswith("$")
    if isinstance(value, Mapping):
        return any(_contains_reference(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_reference(item) for item in value)
    return False


__all__ = ["WorkflowValidation", "WorkflowValidator"]
