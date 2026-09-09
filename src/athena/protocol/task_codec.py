"""Canonical JSON codec for task protocol state.

Task records cross several boundaries (HTTP, ACP, SQLite, and recovery).  All
of those boundaries use this module so a newly added authority field cannot be
silently dropped by one serializer while surviving in another.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Mapping

from athena.protocol.tasks import (
    CapabilityPolicy,
    ContextRef,
    Criterion,
    DeliverySpec,
    ModelPolicy,
    MutationMode,
    NetworkPolicy,
    PathRule,
    ResourceBudget,
    TaskSpec,
    VerificationSpec,
    VerificationType,
    WorkspaceSpec,
)

__all__ = [
    "TaskCodecError",
    "decode_budget",
    "decode_capability_policy",
    "decode_context_refs",
    "decode_criteria",
    "decode_delivery",
    "decode_model_policy",
    "decode_mutation_mode",
    "decode_task_spec",
    "decode_workspace",
    "encode_budget",
    "encode_capability_policy",
    "encode_context_refs",
    "encode_criteria",
    "encode_delivery",
    "encode_model_policy",
    "encode_task_spec",
    "encode_workspace",
]


class TaskCodecError(ValueError):
    """Raised when a task protocol value cannot be decoded safely."""


def _mapping(raw: Any, field: str) -> Mapping[str, Any] | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, Mapping):
        return raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TaskCodecError(f"field '{field}' must be a JSON object") from exc
        if isinstance(value, Mapping):
            return value
    raise TaskCodecError(f"field '{field}' must be a JSON object")


def _items(raw: Any, field: str) -> list[Any]:
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TaskCodecError(f"field '{field}' must be a JSON array") from exc
    if not isinstance(raw, (list, tuple)):
        raise TaskCodecError(f"field '{field}' must be a JSON array")
    return list(raw)


def _int(value: Any, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise TaskCodecError(f"expected an integer, got {value!r}") from exc


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise TaskCodecError(f"expected an integer, got {value!r}") from exc


def _enum(value: Any, enum_type, default):
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value if value is not None else default)
    except ValueError as exc:
        raise TaskCodecError(
            f"invalid {enum_type.__name__}: {value!r}; "
            f"expected one of {[member.value for member in enum_type]}"
        ) from exc


def encode_workspace(workspace: WorkspaceSpec | None) -> str | None:
    if workspace is None:
        return None
    return json.dumps(
        {
            "id": workspace.id,
            "root": workspace.root,
            "readable": [{"path": rule.path, "allow": rule.allow} for rule in workspace.readable],
            "writable": [{"path": rule.path, "allow": rule.allow} for rule in workspace.writable],
            "temp_root": workspace.temp_root,
            "execution_backend": workspace.execution_backend,
            "network_policy": workspace.network_policy.value,
            "mutation_mode": workspace.mutation_mode.value,
            "delegate_mode": workspace.delegate_mode,
            "required_child": workspace.required_child,
            "revision": workspace.revision,
        },
        sort_keys=True,
    )


def decode_workspace(
    raw: Any,
    *,
    id_fallback: str = "",
    network_default: NetworkPolicy = NetworkPolicy.ALLOW,
) -> WorkspaceSpec | None:
    data = _mapping(raw, "workspace")
    if data is None:
        return None
    root = data.get("root")
    if not isinstance(root, str) or not root.strip():
        raise TaskCodecError("field 'root' is required and must be a non-empty string")
    readable = tuple(
        PathRule(path=str(rule.get("path", "")), allow=bool(rule.get("allow", True)))
        for rule in (data.get("readable") or [])
    )
    writable = tuple(
        PathRule(path=str(rule.get("path", "")), allow=bool(rule.get("allow", True)))
        for rule in (data.get("writable") or [])
    )
    return WorkspaceSpec(
        id=str(data.get("id") or id_fallback),
        root=root,
        readable=readable,
        writable=writable,
        temp_root=(str(data["temp_root"]) if data.get("temp_root") is not None else None),
        execution_backend=(
            str(data["execution_backend"]) if data.get("execution_backend") is not None else None
        ),
        network_policy=_enum(data.get("network_policy"), NetworkPolicy, network_default.value),
        mutation_mode=_enum(data.get("mutation_mode"), MutationMode, MutationMode.DIRECT.value),
        delegate_mode=(
            str(data["delegate_mode"]) if data.get("delegate_mode") is not None else None
        ),
        required_child=bool(data.get("required_child", True)),
        revision=(str(data["revision"]) if data.get("revision") is not None else None),
    )


def decode_mutation_mode(raw: Any) -> MutationMode | None:
    """Decode an optional top-level mutation authority field."""
    if raw is None or raw == "":
        return None
    return _enum(raw, MutationMode, MutationMode.DIRECT.value)


def encode_criteria(criteria: tuple[Criterion, ...] | None) -> str:
    result = []
    for criterion in criteria or ():
        verification = criterion.verification
        result.append(
            {
                "id": criterion.id,
                "description": criterion.description,
                "required": criterion.required,
                "evidence_required": criterion.evidence_required,
                "verification": (
                    {
                        "type": verification.type.value,
                        "command": verification.command,
                        "path": verification.path,
                        "predicate": verification.predicate,
                        "capability": verification.capability,
                    }
                    if verification is not None
                    else None
                ),
            }
        )
    return json.dumps(result, sort_keys=True)


def decode_criteria(raw: Any) -> tuple[Criterion, ...]:
    result: list[Criterion] = []
    for index, item in enumerate(_items(raw, "acceptance_criteria")):
        verification: VerificationSpec | None = None
        if isinstance(item, str):
            description = item.strip()
            if not description:
                continue
            if description.casefold().startswith("command:"):
                verification = VerificationSpec(
                    type=VerificationType.COMMAND,
                    command=description.split(":", 1)[1].strip(),
                )
            else:
                verification = VerificationSpec(
                    type=VerificationType.MODEL_JUDGMENT,
                    predicate=description,
                )
            result.append(
                Criterion(
                    id=f"ac_{index + 1}",
                    description=description,
                    verification=verification,
                    required=True,
                    evidence_required=False,
                )
            )
            continue
        if not isinstance(item, Mapping):
            raise TaskCodecError("acceptance criteria entries must be objects")
        verification_data = item.get("verification")
        if verification_data:
            if not isinstance(verification_data, Mapping):
                raise TaskCodecError("criterion verification must be an object")
            verification = VerificationSpec(
                type=_enum(
                    verification_data.get("type"),
                    VerificationType,
                    VerificationType.MODEL_JUDGMENT.value,
                ),
                command=verification_data.get("command"),
                path=verification_data.get("path"),
                predicate=verification_data.get("predicate"),
                capability=verification_data.get("capability"),
            )
        result.append(
            Criterion(
                id=str(item.get("id") or ""),
                description=str(item.get("description") or ""),
                verification=verification,
                required=bool(item.get("required", True)),
                evidence_required=bool(item.get("evidence_required", False)),
            )
        )
    return tuple(result)


def encode_budget(budget: ResourceBudget | None) -> str:
    budget = budget or ResourceBudget()
    return json.dumps(
        {
            "max_agent_iterations": budget.max_agent_iterations,
            "max_input_tokens": budget.max_input_tokens,
            "max_output_tokens": budget.max_output_tokens,
            "max_cost_usd": str(budget.max_cost_usd) if budget.max_cost_usd is not None else None,
            "max_wall_time": (
                budget.max_wall_time.total_seconds() if budget.max_wall_time is not None else None
            ),
            "max_children": budget.max_children,
            "max_child_depth": budget.max_child_depth,
            "max_parallel_model_calls": budget.max_parallel_model_calls,
            "max_parallel_executions": budget.max_parallel_executions,
            "max_artifact_bytes": budget.max_artifact_bytes,
        },
        sort_keys=True,
    )


def decode_budget(raw: Any) -> ResourceBudget:
    data = _mapping(raw, "resource_budget")
    if data is None:
        return ResourceBudget()
    wall = data.get("max_wall_time")
    cost = data.get("max_cost_usd")
    return ResourceBudget(
        max_agent_iterations=_int(data.get("max_agent_iterations"), 50),
        max_input_tokens=_optional_int(data.get("max_input_tokens")),
        max_output_tokens=_optional_int(data.get("max_output_tokens")),
        max_cost_usd=Decimal(str(cost)) if cost not in (None, "") else None,
        max_wall_time=timedelta(seconds=float(wall)) if wall not in (None, "") else None,
        max_children=_int(data.get("max_children"), 4),
        max_child_depth=_int(data.get("max_child_depth"), 1),
        max_parallel_model_calls=_int(data.get("max_parallel_model_calls"), 4),
        max_parallel_executions=_int(data.get("max_parallel_executions"), 16),
        max_artifact_bytes=_int(data.get("max_artifact_bytes"), 100 * 1024 * 1024),
    )


def encode_model_policy(policy: ModelPolicy | None) -> str:
    policy = policy or ModelPolicy()
    return json.dumps(
        {
            "role": policy.role,
            "allowed": list(policy.allowed),
            "require_tools": policy.require_tools,
            "privacy": policy.privacy,
            "max_cost_usd": str(policy.max_cost_usd) if policy.max_cost_usd is not None else None,
            "routing_preference": policy.routing_preference,
            "min_quality_tier": policy.min_quality_tier,
            "require_declared_quality": policy.require_declared_quality,
            "max_model_attempts": policy.max_model_attempts,
        },
        sort_keys=True,
    )


def decode_model_policy(raw: Any) -> ModelPolicy:
    data = _mapping(raw, "model_policy")
    if data is None:
        return ModelPolicy()
    cost = data.get("max_cost_usd")
    return ModelPolicy(
        role=str(data.get("role", "primary")),
        allowed=tuple(str(value) for value in (data.get("allowed") or ())),
        require_tools=bool(data.get("require_tools", False)),
        privacy=str(data.get("privacy", "local-preferred")),
        max_cost_usd=Decimal(str(cost)) if cost not in (None, "") else None,
        routing_preference=str(data.get("routing_preference", "balanced")),
        min_quality_tier=(
            str(data["min_quality_tier"]) if data.get("min_quality_tier") is not None else None
        ),
        require_declared_quality=bool(data.get("require_declared_quality", False)),
        max_model_attempts=int(data.get("max_model_attempts", 2)),
    )


def encode_capability_policy(policy: CapabilityPolicy | None) -> str:
    policy = policy or CapabilityPolicy()
    return json.dumps(
        {
            "effects": sorted(policy.effects),
            "allow": list(policy.allow),
            "ask": list(policy.ask),
            "deny": list(policy.deny),
        },
        sort_keys=True,
    )


def decode_capability_policy(raw: Any) -> CapabilityPolicy:
    data = _mapping(raw, "capability_policy")
    if data is None:
        return CapabilityPolicy()
    return CapabilityPolicy(
        effects=frozenset(str(value) for value in (data.get("effects") or ())),
        allow=tuple(str(value) for value in (data.get("allow") or ())),
        ask=tuple(str(value) for value in (data.get("ask") or ())),
        deny=tuple(str(value) for value in (data.get("deny") or ())),
    )


def encode_context_refs(refs: tuple[ContextRef, ...] | None) -> str:
    return json.dumps(
        [
            {
                "kind": ref.kind,
                "ref": ref.ref,
                "source_id": ref.source_id,
                "summary": ref.summary,
                "mime_type": ref.mime_type,
            }
            for ref in (refs or ())
        ],
        sort_keys=True,
    )


def decode_context_refs(raw: Any) -> tuple[ContextRef, ...]:
    result = []
    for item in _items(raw, "context_refs"):
        if not isinstance(item, Mapping):
            raise TaskCodecError("context_refs entries must be objects")
        result.append(
            ContextRef(
                kind=str(item.get("kind") or "session"),
                ref=str(item.get("ref") or ""),
                source_id=item.get("source_id"),
                summary=item.get("summary"),
                mime_type=item.get("mime_type"),
            )
        )
    return tuple(result)


def encode_delivery(delivery: DeliverySpec | None) -> str | None:
    if delivery is None:
        return None
    return json.dumps(
        {"channel": delivery.channel, "destination": delivery.destination},
        sort_keys=True,
    )


def decode_delivery(raw: Any) -> DeliverySpec | None:
    data = _mapping(raw, "delivery")
    if data is None:
        return None
    return DeliverySpec(channel=data.get("channel"), destination=data.get("destination"))


def encode_task_spec(task: TaskSpec) -> dict[str, Any]:
    """Return the canonical transport/database record for a ``TaskSpec``."""
    workspace = encode_workspace(task.workspace)
    delivery = encode_delivery(task.delivery)
    return {
        "id": task.id,
        "objective": task.objective,
        "session_id": task.session_id,
        "parent_task_id": task.parent_task_id,
        "acceptance_criteria": json.loads(encode_criteria(task.acceptance_criteria)),
        "context_refs": json.loads(encode_context_refs(task.context_refs)),
        "workspace": json.loads(workspace) if workspace is not None else None,
        "capability_policy": json.loads(encode_capability_policy(task.capability_policy)),
        "model_policy": json.loads(encode_model_policy(task.model_policy)),
        "resource_budget": json.loads(encode_budget(task.resource_budget)),
        "deadline": task.deadline.isoformat() if task.deadline else None,
        "delivery": json.loads(delivery) if delivery is not None else None,
        "metadata": dict(task.metadata),
    }


def decode_task_spec(raw: Mapping[str, Any], *, status: str | None = None) -> TaskSpec:
    """Rebuild a ``TaskSpec`` from either a DB row or canonical record."""
    metadata = dict(raw.get("metadata") or {})
    if status is not None:
        metadata["status"] = status
    deadline = raw.get("deadline")
    if isinstance(deadline, str):
        try:
            deadline = datetime.fromisoformat(deadline)
        except ValueError:
            deadline = None
    return TaskSpec(
        id=str(raw["id"]),
        objective=str(raw.get("objective") or ""),
        session_id=raw.get("session_id"),
        parent_task_id=raw.get("parent_task_id"),
        acceptance_criteria=decode_criteria(raw.get("acceptance_criteria")),
        context_refs=decode_context_refs(raw.get("context_refs")),
        workspace=decode_workspace(raw.get("workspace")),
        capability_policy=decode_capability_policy(raw.get("capability_policy")),
        model_policy=decode_model_policy(raw.get("model_policy")),
        resource_budget=decode_budget(raw.get("resource_budget")),
        deadline=deadline,
        delivery=decode_delivery(raw.get("delivery")),
        metadata=metadata,
    )
