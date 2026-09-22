"""Deterministic request-risk predicates for :class:`RealityGate`.

The gate owns disposition and workspace routing. These predicates decide only
whether a concrete request can affect the project workspace or hide execution
behind an opaque runtime. Static vocabulary remains in ``sensitivity.py``.
"""

from __future__ import annotations

from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityRequest,
    CapabilityRequestOrigin,
    EffectClass,
)
from athena.reality.sensitivity import (
    PROCESS_CAPABILITIES,
    PROJECT_CAPABILITIES,
    READ_ONLY_OPERATIONS,
)

__all__ = [
    "is_opaque_execution",
    "is_project_sensitive",
    "is_workspace_bound",
]


def is_project_sensitive(
    request: CapabilityRequest,
    effects,
    descriptor: CapabilityDescriptor,
) -> bool:
    """Classify mutation risk without relying on operation names alone."""
    capability_id = str(request.capability_id)
    operation = str(
        (request.arguments or {}).get("operation") or (request.arguments or {}).get("action") or ""
    ).casefold()
    effect_set = set(effects or ())

    # Arbitrary process/code execution is opaque: it can rewrite a project
    # regardless of whether the source happens to contain a write command.
    if capability_id in PROCESS_CAPABILITIES:
        return operation not in READ_ONLY_OPERATIONS

    # Generated/project affordances execute code supplied by the task and
    # inherit the same boundary even when their declared envelope only
    # contains EXECUTE/READ_LOCAL.
    origin = getattr(descriptor.origin, "value", descriptor.origin)
    if origin in {"generated", "project", "user"} and (
        EffectClass.EXECUTE in effect_set
        or EffectClass.WRITE_LOCAL in effect_set
        or EffectClass.DELETE in effect_set
    ):
        return True

    if capability_id in PROJECT_CAPABILITIES:
        if operation in READ_ONLY_OPERATIONS:
            return False
        return bool(
            effect_set
            & {
                EffectClass.WRITE_LOCAL,
                EffectClass.DELETE,
                EffectClass.EXECUTE,
                EffectClass.SPAWN_PROCESS,
            }
        )

    # A native capability with an explicit local mutation contract is
    # project-sensitive unless it is clearly a read-only operation. This
    # keeps new mutation-capable capabilities safe by default.
    return (
        bool(effect_set & {EffectClass.WRITE_LOCAL, EffectClass.DELETE})
        and operation not in READ_ONLY_OPERATIONS
    )


def is_workspace_bound(
    request: CapabilityRequest,
    descriptor: CapabilityDescriptor,
) -> bool:
    capability_id = str(request.capability_id)
    if capability_id in PROJECT_CAPABILITIES | PROCESS_CAPABILITIES:
        return True
    origin = getattr(descriptor.origin, "value", descriptor.origin)
    return origin in {"generated", "project", "user"}


def is_opaque_execution(
    request: CapabilityRequest,
    effects,
    descriptor: CapabilityDescriptor,
) -> bool:
    """Whether arbitrary code may mutate the workspace undeclared.

    Opaque execution (shell, python, generated code, PTY) can perform
    writes/deletes that the effect descriptor does not express.  Such calls
    MUST enter the candidate path so the RealityGate can observe what actually
    mutated before any effect crosses into reality.  Reads and declared-path
    mutations are not opaque.
    """
    capability_id = str(request.capability_id)
    effect_set = set(effects or ())
    origin = getattr(descriptor.origin, "value", descriptor.origin)
    if (
        capability_id in PROCESS_CAPABILITIES
        or EffectClass.EXECUTE in effect_set
        or EffectClass.SPAWN_PROCESS in effect_set
        or origin in {"generated", "project", "user"}
    ):
        # Trusted orchestration (fusion probes, commit plans) and the
        # acceptance verifier execute under their own authority envelope,
        # explicitly bound to the workspace they pass in — frequently the
        # shadow itself. Reclassifying them would hijack a probe aimed at
        # a candidate branch into a nested candidate and deadlock the
        # commit boundary. The opaque hazard is model-REACHABLE code:
        # MODEL, USER_DIRECT, GENERATED, MCP, REMOTE origins stay opaque.
        request_origin = getattr(request.origin, "value", request.origin)
        if request_origin in {
            CapabilityRequestOrigin.TRUSTED_ORCHESTRATION.value,
            CapabilityRequestOrigin.SYSTEM_VERIFICATION.value,
        }:
            return False
        return True
    return False
