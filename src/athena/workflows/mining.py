"""Small deterministic helpers for learning workflows across task traces."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from athena.workflows.models import Workflow, WorkflowStep


def merge_observation(
    workflow: Workflow,
    *,
    task_id: str,
    steps: Sequence[WorkflowStep],
    max_observations: int = 16,
) -> Workflow:
    """Merge one successful trace and infer only differing input values.

    The procedure shape must already match the candidate's shape.  Values
    that are stable remain literals; values that differ become typed
    ``$input.<name>`` references.  This keeps learned workflows declarative
    and makes generalization auditable rather than model-generated.
    """
    provenance = dict(workflow.provenance)
    task_ids = [str(value) for value in provenance.get("observed_task_ids") or ()]
    if task_id in task_ids:
        return workflow
    observations = _stored_observations(workflow)
    observations.append(
        {
            "task_id": task_id,
            "steps": [step.to_record() for step in steps],
        }
    )
    observations = observations[-max(1, max_observations) :]

    observed_steps = [
        tuple(WorkflowStep.from_record(value, index) for index, value in enumerate(item["steps"]))
        for item in observations
        if isinstance(item, Mapping) and isinstance(item.get("steps"), list)
    ]
    if not observed_steps or any(len(value) != len(observed_steps[0]) for value in observed_steps):
        return _record_only(workflow, provenance, task_id, observations)

    first = observed_steps[0]
    input_values: dict[str, Any] = {}
    input_schema: dict[str, Any] = {}
    input_bindings: dict[str, str] = {}
    used_names: set[str] = set()
    generalized: list[WorkflowStep] = []
    for step_index, first_step in enumerate(first):
        argument_sets = [dict(item[step_index].arguments) for item in observed_steps]
        arguments = _generalize_value(
            argument_sets,
            path=(first_step.id,),
            input_values=input_values,
            input_schema=input_schema,
            input_bindings=input_bindings,
            used_names=used_names,
        )
        generalized.append(replace(first_step, arguments=dict(arguments)))

    task_ids.extend(
        str(item.get("task_id"))
        for item in observations
        if isinstance(item, Mapping) and item.get("task_id")
    )
    unique_task_ids = list(dict.fromkeys(task_ids))[-64:]
    provenance.update(
        {
            "observed_task_ids": unique_task_ids,
            "successful_observations": len(unique_task_ids),
            "observations": observations,
            "generalized": bool(input_schema),
            "representative_inputs": input_values,
            "parameter_bindings": {
                name: {"source_path": path} for name, path in input_bindings.items()
            },
        }
    )
    return replace(
        workflow,
        steps=tuple(generalized),
        input_schema={
            "type": "object",
            "properties": input_schema,
            **({"required": sorted(input_schema)} if input_schema else {}),
        },
        provenance=provenance,
        lifecycle_state="CANDIDATE",
    )


def _stored_observations(workflow: Workflow) -> list[dict[str, Any]]:
    raw = workflow.provenance.get("observations")
    if isinstance(raw, list) and raw:
        return [dict(item) for item in raw if isinstance(item, Mapping)]
    task_id = workflow.provenance.get("task_id") or workflow.task_scope
    return [
        {
            "task_id": str(task_id) if task_id else "unknown",
            "steps": [step.to_record() for step in workflow.steps],
        }
    ]


def _record_only(
    workflow: Workflow,
    provenance: dict[str, Any],
    task_id: str,
    observations: list[dict[str, Any]],
) -> Workflow:
    task_ids = list(
        dict.fromkeys(
            [
                *(str(value) for value in provenance.get("observed_task_ids") or ()),
                task_id,
            ]
        )
    )[-64:]
    provenance.update(
        {
            "observed_task_ids": task_ids,
            "successful_observations": len(task_ids),
            "observations": observations,
        }
    )
    return replace(workflow, provenance=provenance, lifecycle_state="CANDIDATE")


def _generalize_value(
    values: list[Any],
    *,
    path: tuple[str, ...],
    input_values: dict[str, Any],
    input_schema: dict[str, Any],
    input_bindings: dict[str, str],
    used_names: set[str],
) -> Any:
    if values and all(value == values[0] for value in values[1:]):
        return values[0]
    if values and all(isinstance(value, Mapping) for value in values):
        keys = [set(value) for value in values]
        if all(key_set == keys[0] for key_set in keys[1:]):
            return {
                str(key): _generalize_value(
                    [value[key] for value in values],
                    path=(*path, str(key)),
                    input_values=input_values,
                    input_schema=input_schema,
                    input_bindings=input_bindings,
                    used_names=used_names,
                )
                for key in values[0]
            }
    if values and all(isinstance(value, list) for value in values):
        lengths = {len(value) for value in values}
        if len(lengths) == 1:
            return [
                _generalize_value(
                    [value[index] for value in values],
                    path=(*path, str(index)),
                    input_values=input_values,
                    input_schema=input_schema,
                    input_bindings=input_bindings,
                    used_names=used_names,
                )
                for index in range(len(values[0]))
            ]

    name = _input_name(path, used_names)
    used_names.add(name)
    input_values[name] = values[0] if values else None
    input_schema[name] = _schema_for(values)
    input_bindings[name] = ".".join((*path[:-1], "arguments", path[-1]))
    return f"$input.{name}"


def _input_name(path: tuple[str, ...], used: set[str]) -> str:
    hint = _slug(path[-1] if path else "value") or "value"
    if hint not in used:
        return hint
    prefix = _slug(path[-2]) if len(path) > 1 else "step"
    candidate = f"{prefix}_{hint}" or "value"
    if candidate not in used:
        return candidate
    index = 2
    while f"{candidate}_{index}" in used:
        index += 1
    return f"{candidate}_{index}"


def _slug(value: Any) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", str(value)).strip("_").lower()


def _schema_for(values: list[Any]) -> dict[str, Any]:
    if not values:
        return {}
    types = {type(value) for value in values}
    if types <= {bool}:
        return {"type": "boolean"}
    if types <= {int}:
        return {"type": "integer"}
    if types <= {int, float}:
        return {"type": "number"}
    if types <= {str}:
        return {"type": "string"}
    if types <= {list}:
        return {"type": "array"}
    if types <= {dict}:
        return {"type": "object"}
    return {}


__all__ = ["merge_observation"]
