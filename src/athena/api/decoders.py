"""JSON -> typed protocol decoders shared across boundary transports (INV-007).

The HTTP API and the ACP adapter both translate JSON-shaped request fragments
into :mod:`athena.protocol.tasks` objects. They MUST agree on decoding so a
client's intent is interpreted identically regardless of transport. Decoding
never broadens a provided limit: every field that is supplied is honoured, and
missing optional fields fall back to the protocol default (never "unlimited"
beyond the documented default).
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from typing import Any, Mapping

from athena.protocol.tasks import (
    CapabilityPolicy,
    ModelPolicy,
    MutationMode,
    NetworkPolicy,
    PathRule,
    ResourceBudget,
    WorkspaceSpec,
)

__all__ = [
    "DecodeError",
    "decode_workspace",
    "decode_capability_policy",
    "decode_model_policy",
    "decode_budget",
]


class DecodeError(ValueError):
    """Raised when a JSON fragment cannot be decoded into a protocol object."""


def _as_mapping(raw: Any, field: str) -> Mapping[str, Any] | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, Mapping):
        return raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DecodeError(f"field '{field}' must be a JSON object") from exc
        if not isinstance(value, Mapping):
            raise DecodeError(f"field '{field}' must be a JSON object")
        return value
    raise DecodeError(f"field '{field}' must be a JSON object")


def _require(data: Mapping[str, Any], field: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value.strip():
        raise DecodeError(f"field '{field}' is required and must be a non-empty string")
    return value


def decode_workspace(
    raw: Any,
    *,
    id_fallback: str = "",
    network_default: NetworkPolicy = NetworkPolicy.ALLOW,
) -> WorkspaceSpec | None:
    """Decode a workspace fragment into a :class:`WorkspaceSpec`.

    ``root`` is required whenever a workspace object is supplied. Missing or
    empty ``root`` raises :class:`DecodeError` (a partial workspace is never
    silently widened).
    """
    data = _as_mapping(raw, "workspace")
    if data is None:
        return None
    root = _require(data, "root")
    id_value = data.get("id")
    workspace_id = str(id_value) if id_value else id_fallback
    readable = tuple(
        PathRule(path=r.get("path", ""), allow=bool(r.get("allow", True)))
        for r in (data.get("readable") or [])
    )
    writable = tuple(
        PathRule(path=r.get("path", ""), allow=bool(r.get("allow", True)))
        for r in (data.get("writable") or [])
    )
    net = data.get("network_policy", network_default)
    if not isinstance(net, NetworkPolicy):
        try:
            net = NetworkPolicy(str(net))
        except ValueError as exc:
            raise DecodeError(
                f"field 'network_policy' must be one of {[p.value for p in NetworkPolicy]}"
            ) from exc
    mutation_mode = data.get("mutation_mode", MutationMode.DIRECT)
    if not isinstance(mutation_mode, MutationMode):
        try:
            mutation_mode = MutationMode(str(mutation_mode))
        except ValueError as exc:
            raise DecodeError(
                f"field 'mutation_mode' must be one of {[m.value for m in MutationMode]}"
            ) from exc
    return WorkspaceSpec(
        id=workspace_id,
        root=root,
        readable=readable,
        writable=writable,
        temp_root=data.get("temp_root"),
        execution_backend=(
            str(data["execution_backend"]) if data.get("execution_backend") is not None else None
        ),
        network_policy=net,
        mutation_mode=mutation_mode,
        revision=(str(data["revision"]) if data.get("revision") is not None else None),
    )


def decode_capability_policy(raw: Any) -> CapabilityPolicy:
    """Decode a capability policy fragment (empty list => empty allow-list)."""
    data = _as_mapping(raw, "capability_policy")
    if data is None:
        return CapabilityPolicy()
    effects = frozenset(data.get("effects") or [])
    return CapabilityPolicy(
        effects=effects,
        allow=tuple(data.get("allow") or ()),
        ask=tuple(data.get("ask") or ()),
        deny=tuple(data.get("deny") or ()),
    )


def decode_model_policy(raw: Any) -> ModelPolicy:
    """Decode a model policy fragment, always honoring supplied fields."""
    data = _as_mapping(raw, "model_policy")
    if data is None:
        return ModelPolicy()
    cost = data.get("max_cost_usd")
    return ModelPolicy(
        role=str(data.get("role", "primary")),
        allowed=tuple(data.get("allowed") or ()),
        require_tools=bool(data.get("require_tools", True)),
        privacy=str(data.get("privacy", "local-preferred")),
        max_cost_usd=Decimal(str(cost)) if cost else None,
        routing_preference=str(data.get("routing_preference", "balanced")),
    )


def _opt_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise DecodeError(f"expected an integer, got {value!r}") from exc


def _int(value: Any, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise DecodeError(f"expected an integer, got {value!r}") from exc


def decode_budget(raw: Any) -> ResourceBudget:
    """Decode a full :class:`ResourceBudget`, mapping EVERY field.

    A client's supplied limits are never dropped: every present field maps onto
    the corresponding budget attribute. Fields that the protocol models as
    optional (``None`` default) stay optional; scalar fields use the documented
    default (never "unlimited") when absent.
    """
    data = _as_mapping(raw, "resource_budget")
    if data is None:
        return ResourceBudget()

    wall = data.get("max_wall_time")
    cost = data.get("max_cost_usd")
    return ResourceBudget(
        max_agent_iterations=_int(data.get("max_agent_iterations"), 500),
        max_input_tokens=_opt_int(data.get("max_input_tokens")),
        max_output_tokens=_opt_int(data.get("max_output_tokens")),
        max_cost_usd=Decimal(str(cost)) if cost else None,
        max_wall_time=timedelta(seconds=float(wall)) if wall else None,
        max_children=_int(data.get("max_children"), 16),
        max_child_depth=_int(data.get("max_child_depth"), 1),
        max_parallel_model_calls=_int(data.get("max_parallel_model_calls"), 4),
        max_parallel_executions=_int(data.get("max_parallel_executions"), 16),
        max_artifact_bytes=_int(data.get("max_artifact_bytes"), 100 * 1024 * 1024),
    )
