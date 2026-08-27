"""Model-visible bounded access to Athena's fusion machinery."""

from __future__ import annotations

import dataclasses
import json
from typing import Any

from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityOrigin,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)


class FusionCapability:
    """Expose experiments, forks, checkpoints, and branch control.

    Fusion remains one-agent orchestration: this capability delegates to the
    service-owned :class:`FusionOrchestrator`, which uses the normal dispatcher
    for shadow execution, verification, and real-workspace commit.
    """

    descriptor = CapabilityDescriptor(
        id="fusion",
        description=(
            "Run bounded speculative experiments in a shadow workspace, inspect "
            "or discard branches, commit verified changes, create causal forks, "
            "and capture workspace checkpoints."
        ),
        input_schema={
            "type": "object", "required": ["operation"],
            "properties": {
                "operation": {"type": "string", "enum": [
                    "run", "status", "commit", "discard", "fork", "checkpoint",
                ]},
                "branch_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "proposal": {
                    "type": "array", "minItems": 1, "maxItems": 100,
                    "items": {
                        "type": "object", "required": ["capability_id", "arguments"],
                        "properties": {
                            "capability_id": {"type": "string", "minLength": 1, "maxLength": 128},
                            "arguments": {"type": "object"},
                        },
                        "additionalProperties": False,
                    },
                },
                "criteria_probes": {
                    "type": "array", "maxItems": 100,
                    "items": {"type": "object", "additionalProperties": True},
                },
                "invariants": {
                    "type": "array", "maxItems": 100,
                    "items": {"type": "object", "additionalProperties": True},
                },
                "profile": {"type": "string", "maxLength": 128},
                "auto_fork_on_failure": {"type": "boolean"},
                "after_event_sequence": {"type": "integer", "minimum": 0},
                "capture_checkpoint": {"type": "boolean"},
                "checkpoint_id": {"type": "string"},
                "label": {"type": "string"},
                "reason": {"type": "string"},
            },
            "oneOf": [
                {"properties": {"operation": {"const": "run"}},
                 "required": ["proposal"]},
                {"properties": {"operation": {"enum": ["status", "commit", "discard"]}},
                 "required": ["branch_id"]},
                {"properties": {"operation": {"const": "fork"}},
                 "required": ["after_event_sequence"]},
                {"properties": {"operation": {"const": "checkpoint"}}},
            ],
            "additionalProperties": False,
        },
        effects=frozenset({
            EffectClass.READ_LOCAL, EffectClass.WRITE_LOCAL, EffectClass.DELETE,
            EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS,
        }),
        origin=CapabilityOrigin.NATIVE,
    )

    def __init__(self, service: Any) -> None:
        self._service = service

    async def invoke(self, request: CapabilityRequest, *, context=None, **kw):
        args = dict(request.arguments or {})
        operation = str(args.get("operation") or "")
        task_id = request.task_id
        if task_id is None:
            return _result(request, ok=False, error="fusion requires a task scope")
        try:
            orchestrator = self._service.fusion_orchestrator()
            if operation == "run":
                proposal = [dict(item) for item in args.get("proposal") or ()]
                if not proposal:
                    return _result(request, ok=False, error="run requires a non-empty proposal")
                outcome = await orchestrator.run_experiment(
                    task_id=task_id,
                    proposal=proposal,
                    criteria_probes=[dict(item) for item in args.get("criteria_probes") or ()],
                    invariants=[dict(item) for item in args.get("invariants") or ()],
                    profile=args.get("profile"),
                    auto_fork_on_failure=bool(args.get("auto_fork_on_failure", True)),
                )
                return _result(request, output=json.dumps(dataclasses.asdict(outcome)))

            branch_id = str(args.get("branch_id") or "")
            if operation == "status":
                branch = _owned_branch(orchestrator, branch_id, task_id)
                if branch is None:
                    return _result(request, ok=False, error="branch not found")
                return _result(request, output=json.dumps(_branch_record(branch)))
            if operation == "discard":
                branch = _owned_branch(orchestrator, branch_id, task_id)
                if branch is None:
                    return _result(request, ok=False, error="branch not found")
                outcome = await orchestrator.shadow.discard(
                    branch, reason=str(args.get("reason") or "discarded by operator")
                )
                return _result(request, output=json.dumps(outcome))
            if operation == "commit":
                branch = _owned_branch(orchestrator, branch_id, task_id)
                if branch is None:
                    return _result(request, ok=False, error="branch not found")
                outcome = await orchestrator.shadow.commit(branch)
                return _result(request, output=json.dumps(outcome))
            if operation == "fork":
                if "after_event_sequence" not in args:
                    return _result(request, ok=False,
                                   error="fork requires after_event_sequence")
                outcome = await orchestrator.fork_from_event(
                    task_id=task_id,
                    after_event_sequence=int(args["after_event_sequence"]),
                    capture_checkpoint=bool(args.get("capture_checkpoint", False)),
                    checkpoint_id=args.get("checkpoint_id"),
                )
                return _result(request, output=json.dumps(outcome))
            if operation == "checkpoint":
                workspace = getattr(context, "workspace", None)
                if workspace is None:
                    return _result(request, ok=False,
                                   error="checkpoint requires workspace context")
                outcome = await orchestrator.checkpoints.capture(
                    task_id=task_id,
                    workspace_root=workspace.root,
                    label=str(args.get("label") or "operator checkpoint"),
                )
                return _result(request, output=json.dumps(outcome))
            return _result(request, ok=False, error=f"unknown fusion operation: {operation}")
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            return _result(request, ok=False, error=str(exc))


def _branch_record(branch: Any) -> dict[str, Any]:
    return {
        "id": branch.id,
        "task_id": branch.task_id,
        "status": branch.status,
        "proposal": branch.proposal,
        "verification": branch.verification,
        "mutations": branch.mutations,
        "commit_plan": branch.commit_plan,
        "commit_outcome": branch.commit_outcome,
        "commit_state": branch.commit_state,
        "commit_started_at": branch.commit_started_at,
        "commit_completed_at": branch.commit_completed_at,
        "checkpoint_id": branch.checkpoint_id,
        "error": branch.error,
        "policy_profile": branch.policy_profile,
        "created_at": branch.created_at,
    }


def _owned_branch(orchestrator: Any, branch_id: str, task_id: str) -> Any | None:
    branch = orchestrator.shadow.get_branch(branch_id)
    if branch is None or branch.task_id != task_id:
        return None
    return branch


def _result(request, *, ok: bool = True, output: str = "", error: str | None = None):
    return CapabilityResult(
        request.call_id, request.capability_id,
        CapabilityResultStatus.OK if ok else CapabilityResultStatus.FAILED,
        output=output, error=error,
    )


__all__ = ["FusionCapability"]
