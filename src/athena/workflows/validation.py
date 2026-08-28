"""Static validation for declarative workflow graphs."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from athena.capabilities.registry import _compile_validator
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
            _compile_validator(dict(workflow.input_schema))
            if workflow.output_schema is not None:
                _compile_validator(dict(workflow.output_schema))
        except (TypeError, ValueError) as exc:
            errors.append(f"workflow input_schema: {exc}")

        def walk(current: Workflow, path: tuple[str, ...]) -> None:
            if current.id in path:
                errors.append("workflow cycle: " + " -> ".join((*path, current.id)))
                return
            if not current.enabled:
                errors.append(f"workflow {current.id} is disabled")
                return
            step_ids = [step.id for step in current.steps]
            known_steps = set(step_ids)
            for step_id in sorted({item for item in step_ids if step_ids.count(item) > 1}):
                errors.append(f"workflow {current.id}: duplicate step id {step_id}")
            dependency_edges: dict[str, tuple[str, ...]] = {}
            for step in current.steps:
                dependency_edges[step.id] = tuple(step.depends_on)
                for dependency in step.depends_on:
                    if dependency not in known_steps:
                        errors.append(f"{step.id}: unknown dependency {dependency}")
            visiting: set[str] = set()
            visited: set[str] = set()

            def visit(step_id: str, trail: tuple[str, ...] = ()) -> None:
                if step_id in visiting:
                    cycle_start = trail.index(step_id) if step_id in trail else 0
                    errors.append(
                        "step dependency cycle: " + " -> ".join((*trail[cycle_start:], step_id))
                    )
                    return
                if step_id in visited or step_id not in dependency_edges:
                    return
                visiting.add(step_id)
                for dependency in dependency_edges[step_id]:
                    visit(dependency, (*trail, step_id))
                visiting.remove(step_id)
                visited.add(step_id)

            for step_id in step_ids:
                visit(step_id)
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
        return WorkflowValidation(
            ok=not errors, errors=tuple(errors), effects=tuple(sorted(effects))
        )


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
