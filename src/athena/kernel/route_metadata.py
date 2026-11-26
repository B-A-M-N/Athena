"""Provider-route metadata and replay-boundary projection.

These helpers describe a selected provider route and compare durable replay
receipts with that route. They are subordinate to ``AgentKernel`` and
``InferenceBroker``: they project metadata only and never select a route or
authorize an inference call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from athena.context.compiler import CompiledContext
from athena.models.router import ModelSelection

if TYPE_CHECKING:
    from athena.kernel.kernel import AgentKernel

__all__ = ["inference_metadata", "replay_metadata"]


def inference_metadata(
    kernel: AgentKernel,
    selection: ModelSelection,
) -> dict[str, Any]:
    """Describe the configured wire/cache route for one model selection."""
    profile = kernel._registry.profile_for(selection.provider)
    if profile is None:
        return {"provider_profile_id": selection.provider}
    fingerprint = getattr(profile, "fingerprint", None)
    profile_fingerprint = (
        fingerprint() if callable(fingerprint) else str(getattr(profile, "id", selection.provider))
    )
    profile_id = str(getattr(profile, "id", selection.provider))
    model_profile = kernel._registry.model_profile_for(selection.provider, selection.model)
    from athena.models.compat.profiles import resolve_compatibility_profile

    compatibility = resolve_compatibility_profile(
        str(getattr(profile, "compatibility_profile", "auto"))
    )
    return {
        # ID is the stable configured route identity. The fingerprint is
        # separate because changing a route's wire semantics must still
        # create an explicit cache/replay boundary for the same ID.
        "provider_profile_id": profile_id,
        "provider_profile_fingerprint": profile_fingerprint,
        "profile_id": getattr(profile, "id", selection.provider),
        "cache_mode": getattr(profile, "cache_mode", "none"),
        "cache_session_key": (
            f"{selection.provider}:{selection.model}"
            if getattr(profile, "cache_session_key", False)
            else None
        ),
        "compatibility_profile": getattr(profile, "compatibility_profile", "auto"),
        "tool_repair_mode": compatibility.tool_repair,
        "max_tool_correction_cycles": compatibility.max_tool_correction_cycles,
        "protocol": getattr(profile, "protocol", "openai-compat"),
        "idempotency_semantics": getattr(profile, "idempotency_semantics", "none"),
        "model_profile": (dict(vars(model_profile)) if model_profile is not None else None),
    }


def replay_metadata(
    kernel: AgentKernel,
    compiled: CompiledContext,
    selection: ModelSelection,
) -> dict[str, Any]:
    """Project durable replay receipts and an explicit route boundary."""
    receipts: list[dict[str, Any]] = []
    for message in compiled.messages:
        value = (message.metadata or {}).get("inference_receipt")
        if isinstance(value, dict):
            receipts.append(dict(value))
    if not receipts:
        return {"replay_receipts": (), "replay_compatible": True}
    last = receipts[-1]
    current_profile = str(
        inference_metadata(kernel, selection).get("provider_profile_id", selection.provider)
    )
    last_profile = str(last.get("provider_profile_id") or "")
    last_model = str(last.get("model_id") or "")
    boundary = None
    if last_profile and last_profile != current_profile:
        boundary = {
            "reason": "provider_profile_changed",
            "from": last_profile,
            "to": current_profile,
        }
    elif last_model and last_model != selection.model:
        boundary = {
            "reason": "model_changed",
            "from": last_model,
            "to": selection.model,
        }
    return {
        "replay_receipts": tuple(receipts[-8:]),
        "replay_compatible": boundary is None,
        "replay_boundary": boundary,
        "boundary": boundary,
    }
