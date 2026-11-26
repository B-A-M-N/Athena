"""Service-owned schedule authority snapshot helpers."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from athena.protocol.capabilities import EffectClass
from athena.protocol.tasks import (
    DeliverySpec,
    ModelPolicy,
    ResourceBudget,
    ResourceBudgetCeiling,
)


def _authority_digest(authority: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(dict(authority), sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _budget_record(budget: ResourceBudget | ResourceBudgetCeiling) -> dict[str, Any]:
    record: dict[str, Any] = {}
    for name in (
        "max_agent_iterations",
        "max_input_tokens",
        "max_output_tokens",
        "max_cost_usd",
        "max_wall_time",
        "max_children",
        "max_child_depth",
        "max_parallel_model_calls",
        "max_parallel_executions",
        "max_artifact_bytes",
    ):
        value = getattr(budget, name, None)
        if value is not None:
            record[name] = value
    return record


def _model_policy_record(policy: ModelPolicy) -> dict[str, Any]:
    return {
        "role": policy.role,
        "allowed": list(policy.allowed),
        "require_tools": policy.require_tools,
        "privacy": policy.privacy,
        "max_cost_usd": str(policy.max_cost_usd) if policy.max_cost_usd else None,
        "routing_preference": policy.routing_preference,
        "min_quality_tier": policy.min_quality_tier,
        "require_declared_quality": policy.require_declared_quality,
        "max_model_attempts": policy.max_model_attempts,
    }


def _authority_snapshot(
    *,
    workspace: Any,
    capability_policy: Any,
    model_policy: Any,
    resource_budget: Any,
    autonomy: Any,
    delivery: DeliverySpec | None,
    owner: Mapping[str, Any],
) -> dict[str, Any]:
    """Serialize the service-owned ceiling captured at schedule creation."""
    workspace_record: dict[str, Any] = {}
    if workspace is not None:
        workspace_record = {
            "id": getattr(workspace, "id", None),
            "root": getattr(workspace, "root", None),
            "readable": [
                {"path": rule.path, "allow": rule.allow}
                for rule in getattr(workspace, "readable", ())
            ],
            "writable": [
                {"path": rule.path, "allow": rule.allow}
                for rule in getattr(workspace, "writable", ())
            ],
            "temp_root": getattr(workspace, "temp_root", None),
            "execution_backend": getattr(workspace, "execution_backend", None),
            "revision": getattr(workspace, "revision", None),
            "network_policy": getattr(
                getattr(workspace, "network_policy", None),
                "value",
                getattr(workspace, "network_policy", None),
            ),
            "mutation_mode": getattr(
                getattr(workspace, "mutation_mode", None),
                "value",
                getattr(workspace, "mutation_mode", None),
            ),
        }
    cp = capability_policy
    if cp is None:
        # Direct store/API callers have not presented a creator authority
        # snapshot; a persisted schedule must not silently become unrestricted.
        class _SafePolicy:
            effects = ()
            allow = ()
            ask = ()
            deny = ("*",)

        cp = _SafePolicy()
    mp = model_policy
    budget = resource_budget
    delivery_record = None
    delivery_effects: list[str] = []
    if delivery is not None:
        destination = str(delivery.destination or "")
        parts = urlsplit(destination)
        canonical = urlunsplit(
            (parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, "")
        )
        delivery_record = {
            "channel": delivery.channel,
            "destination": destination,
            "destination_hash": hashlib.sha256(canonical.encode()).hexdigest(),
            "credential_identity": None,
        }
        if str(delivery.channel or "").strip().lower() == "webhook":
            delivery_effects = [
                EffectClass.NETWORK_WRITE.value,
                EffectClass.EXTERNAL_MESSAGE.value,
            ]
    return {
        "principal": dict(owner),
        "workspace": workspace_record,
        "capability_policy": {
            "effects": sorted(
                str(getattr(value, "value", value)) for value in getattr(cp, "effects", ()) or ()
            ),
            "allow": list(getattr(cp, "allow", ()) or ()),
            "ask": list(getattr(cp, "ask", ()) or ()),
            "deny": list(getattr(cp, "deny", ()) or ()),
        },
        "model_policy": {
            **_model_policy_record(mp if mp is not None else ModelPolicy()),
        },
        "resource_budget": _budget_record(budget) if budget is not None else {},
        "autonomy": getattr(autonomy, "value", autonomy) or "supervised",
        "delivery": delivery_record,
        "delivery_effects": delivery_effects,
        "effect_ceiling": list(delivery_effects),
    }
