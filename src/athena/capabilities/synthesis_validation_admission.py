"""Validate and admit generated capability candidates."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from athena.protocol.capabilities import CapabilityRequest


async def validate_and_admit(
    capability: Any,
    request: CapabilityRequest,
    args: Mapping[str, Any],
    context: Any,
    cap: Any,
    validation_cases: Any,
    compatibility: str,
):
    """Run engine validation and register a passing candidate in the fabric."""
    from athena.capabilities.synthesis_results import synthesis_result as _result

    cap = await capability._engine.validate(
        cap,
        validation_cases,
        tier=str(args.get("validation_tier") or "task"),
        workspace_root=(
            getattr(getattr(context, "workspace", None), "root", None)
            if context is not None
            else None
        ),
        workspace=getattr(context, "workspace", None),
        task_id=request.task_id,
        session_id=request.session_id,
        profile=getattr(context, "autonomy", None),
        task_policy=getattr(context, "capability_policy", None),
        task_budget=getattr(context, "resource_budget", None),
        generated_call_depth=getattr(context, "generated_call_depth", 0),
        generated_call_chain=tuple(getattr(context, "generated_call_chain", ())),
    )
    evidence = await capability._engine.evidence_status(cap, capability._research)
    cap.validation["evidence"] = evidence
    cap.validation["compatibility"] = compatibility
    if evidence["status"] != "CURRENT":
        return _result(
            request,
            ok=False,
            error="generated capability evidence is stale or unavailable",
            output=json.dumps(
                {
                    "capability_id": cap.id,
                    "evidence": evidence,
                }
            ),
            metadata={"capability_id": cap.id, "evidence": evidence},
        )
    if not cap.validation.get("all_passed"):
        return _result(
            request,
            ok=False,
            error="generated capability validation failed",
            output=json.dumps({"capability_id": cap.id, "validation": cap.validation}),
            metadata={"capability_id": cap.id, "validation": cap.validation},
        )
    try:
        admitted = capability._engine.register_ephemeral(capability._fabric, cap)
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        return _result(request, ok=False, error=f"generated capability admission failed: {exc}")
    if not admitted:
        return _result(request, ok=False, error="generated capability was not admitted")
    proof = capability._engine.proof_for(cap.id) or {}
    return _result(
        request,
        output=json.dumps({"capability_id": cap.id, "proof": proof}),
        metadata={
            "capability_id": cap.id,
            "proof": proof,
            "scope": "task",
            **({"supersedes": list(cap.supersedes)} if cap.supersedes else {}),
        },
    )
