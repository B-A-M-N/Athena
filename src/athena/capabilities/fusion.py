"""Model-visible bounded access to Athena's fusion machinery."""

from __future__ import annotations
from athena.capabilities.operations import native_descriptor

import dataclasses
import json
from typing import Any

from athena.protocol.capabilities import (
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
    for shadow execution and verification. It never promotes a candidate into
    reality; the kernel's RealityCoordinator owns candidate→reality promotion.
    """

    descriptor = native_descriptor(
        id="fusion",
        description=(
            "Run bounded speculative experiments in a shadow workspace, inspect "
            "or discard candidates, create causal forks, and capture workspace "
            "checkpoints. Verified candidates remain awaiting reality promotion."
        ),
        input_schema={
            "type": "object",
            "required": ["operation"],
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": [
                        "run",
                        "compare",
                        "status",
                        "discard",
                        "fork",
                        "checkpoint",
                        "inspect_checkpoint",
                        "release_checkpoint",
                    ],
                },
                "branch_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "proposal": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 100,
                    "items": {
                        "type": "object",
                        "required": ["capability_id", "arguments"],
                        "properties": {
                            "capability_id": {"type": "string", "minLength": 1, "maxLength": 128},
                            "arguments": {"type": "object"},
                        },
                        "additionalProperties": False,
                    },
                },
                "proposals": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 8,
                    "items": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 100,
                        "items": {
                            "type": "object",
                            "required": ["capability_id", "arguments"],
                            "properties": {
                                "capability_id": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 128,
                                },
                                "arguments": {"type": "object"},
                            },
                            "additionalProperties": False,
                        },
                    },
                },
                "invariants": {
                    "type": "array",
                    "maxItems": 100,
                    "items": {"type": "object", "additionalProperties": True},
                },
                "profile": {"type": "string", "maxLength": 128},
                "auto_fork_on_failure": {"type": "boolean"},
                "after_event_sequence": {"type": "integer", "minimum": 0},
                "capture_checkpoint": {"type": "boolean"},
                "checkpoint_id": {"type": "string"},
                "label": {"type": "string"},
                "reason": {"type": "string"},
                "changes_from_previous": {"type": "string", "minLength": 1, "maxLength": 2000},
            },
            "oneOf": [
                {"properties": {"operation": {"const": "run"}}, "required": ["proposal"]},
                {"properties": {"operation": {"const": "compare"}}, "required": ["proposals"]},
                {
                    "properties": {"operation": {"enum": ["status", "discard"]}},
                    "required": ["branch_id"],
                },
                {
                    "properties": {"operation": {"const": "fork"}},
                    "required": ["after_event_sequence"],
                },
                {
                    "properties": {"operation": {"const": "inspect_checkpoint"}},
                    "required": ["checkpoint_id"],
                },
                {
                    "properties": {"operation": {"const": "release_checkpoint"}},
                    "required": ["checkpoint_id"],
                },
                {"properties": {"operation": {"const": "checkpoint"}}},
            ],
            "additionalProperties": False,
        },
        effects=frozenset(
            {
                EffectClass.READ_LOCAL,
                EffectClass.WRITE_LOCAL,
                EffectClass.DELETE,
                EffectClass.EXECUTE,
                EffectClass.SPAWN_PROCESS,
            }
        ),
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
                    invariants=[dict(item) for item in args.get("invariants") or ()],
                    profile=args.get("profile"),
                    auto_fork_on_failure=bool(args.get("auto_fork_on_failure", True)),
                )
                return _result(
                    request,
                    output=json.dumps(dataclasses.asdict(outcome)),
                    metadata={
                        "operation": operation,
                        "branch_id": outcome.branch_id,
                        "speculative_status": outcome.status,
                        "diagnostic": dict(outcome.failure_record.get("diagnostic") or {}),
                        "failure_record": dict(outcome.failure_record),
                        "changes_from_previous": str(args.get("changes_from_previous") or ""),
                    },
                )
            if operation == "compare":
                proposals = [
                    [dict(step) for step in proposal] for proposal in args.get("proposals") or ()
                ]
                if len(proposals) < 2:
                    return _result(
                        request,
                        ok=False,
                        error="compare requires at least two proposals",
                    )
                outcome = await orchestrator.compare(
                    task_id=task_id,
                    proposals=proposals,
                    invariants=[dict(item) for item in args.get("invariants") or ()],
                    profile=args.get("profile"),
                )
                return _result(
                    request,
                    output=json.dumps(outcome),
                    metadata={
                        "operation": operation,
                        "comparison_id": outcome.get("comparison_id"),
                        "verified_count": outcome.get("verified_count", 0),
                        "comparative_evidence": {
                            str(item.get("branch_id")): dict(item.get("comparative_evidence") or {})
                            for item in outcome.get("candidates", ())
                            if item.get("branch_id")
                        }
                    },
                )

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
            if operation == "fork":
                if "after_event_sequence" not in args:
                    return _result(request, ok=False, error="fork requires after_event_sequence")
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
                    return _result(request, ok=False, error="checkpoint requires workspace context")
                outcome = await orchestrator.capture_checkpoint(
                    task_id=task_id,
                    workspace_root=workspace.root,
                    label=str(args.get("label") or "operator checkpoint"),
                )
                return _result(request, output=json.dumps(outcome))
            if operation == "inspect_checkpoint":
                checkpoint_id = str(args.get("checkpoint_id") or "")
                if not checkpoint_id:
                    return _result(
                        request,
                        ok=False,
                        error="checkpoint_id is required",
                    )
                outcome = await orchestrator.checkpoints.inspect(checkpoint_id)
                if not _checkpoint_owned_by_task(outcome, task_id):
                    return _result(request, ok=False, error="checkpoint not found")
                return _result(request, output=json.dumps(outcome))
            if operation == "release_checkpoint":
                checkpoint_id = str(args.get("checkpoint_id") or "")
                if not checkpoint_id:
                    return _result(
                        request,
                        ok=False,
                        error="checkpoint_id is required",
                    )
                inspected = await orchestrator.checkpoints.inspect(checkpoint_id)
                if not _checkpoint_owned_by_task(inspected, task_id):
                    return _result(request, ok=False, error="checkpoint not found")
                released = await orchestrator.checkpoints.release(
                    checkpoint_id,
                    owner=task_id,
                )
                return _result(
                    request,
                    output=json.dumps(
                        {
                            "checkpoint_id": checkpoint_id,
                            "released": True,
                            "deleted": bool(released),
                        }
                    ),
                )
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


def _checkpoint_owned_by_task(checkpoint: Any, task_id: str) -> bool:
    """Keep checkpoint inspection/release inside the creating task scope.

    Older checkpoint adapters and small test doubles may omit ``task_id``;
    those are accepted for compatibility. Durable manifests always include
    it, so a real checkpoint cannot be used to probe another task's snapshot.
    """
    if not isinstance(checkpoint, dict):
        return False
    owner = checkpoint.get("task_id")
    return owner is None or str(owner) == task_id


def _result(
    request,
    *,
    ok: bool = True,
    output: str = "",
    error: str | None = None,
    metadata: dict[str, Any] | None = None,
):
    return CapabilityResult(
        request.call_id,
        request.capability_id,
        CapabilityResultStatus.OK if ok else CapabilityResultStatus.FAILED,
        output=output,
        error=error,
        metadata=metadata or {},
    )


__all__ = ["FusionCapability"]
