"""Branch and checkpoint routing mechanics for :class:`RealityGate`.

``RealityGate`` remains the authority that classifies a request and chooses a
disposition. This helper only performs the selected branch/checkpoint
operation against the gate's existing state and never chooses a tier.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from athena.causal.checkpoint import run_checkpoint_worker
from athena.protocol.capabilities import CapabilityDescriptor, CapabilityRequest, EffectClass
from athena.protocol.messages import utcnow
from athena.protocol.reality import ExecutionDisposition
from athena.protocol.tasks import MutationMode, WorkspaceSpec
from athena.reality.routing import RealityRoute, translate_workspace_arguments
from athena.reality.request_risk import (
    is_opaque_execution,
    is_project_sensitive,
    is_workspace_bound,
)

__all__ = ["RealityRouting", "planned_postconditions", "transaction_resources"]


class RealityRouting:
    """Perform one route selected by the owning ``RealityGate``."""

    def __init__(self, gate: Any) -> None:
        self._gate = gate

    async def route_request(
        self,
        request: CapabilityRequest,
        workspace: WorkspaceSpec,
        effects: Mapping[EffectClass, Any]
        | frozenset[EffectClass]
        | tuple[EffectClass, ...]
        | set[EffectClass],
        descriptor: CapabilityDescriptor,
        tier: str | None = None,
    ) -> RealityRoute:
        return await route_request(
            self._gate,
            request,
            workspace,
            effects,
            descriptor,
            tier=tier,
        )

    async def isolated(
        self,
        request: CapabilityRequest,
        workspace: WorkspaceSpec,
        descriptor: Any,
    ) -> RealityRoute:
        """Open a per-call ephemeral shadow, discarded independently."""
        del descriptor
        call_id = request.call_id
        if call_id is None:
            return await self.speculative(request, workspace)
        gate = self._gate
        lock = gate._state.locks.lock(call_id)
        async with lock:
            branch = gate._branches.ephemeral_branch(call_id)
            if branch is None:
                branch = await gate._shadow.open_branch(
                    task_id=request.task_id,
                    base_workspace=workspace,
                    proposal=[],
                )
                gate._branches.set_ephemeral(call_id, branch)
            target = branch.shadow_workspace
            translate_workspace_arguments(request, workspace.root, target.root)
            return RealityRoute(target, ExecutionDisposition.ISOLATED, transaction_id=branch.id)

    async def transactional(
        self,
        request: CapabilityRequest,
        workspace: WorkspaceSpec,
    ) -> RealityRoute:
        """Capture a real-workspace checkpoint, then mutate in place."""
        gate = self._gate
        task_id = request.task_id
        if task_id is None or gate._checkpoints is None:
            return await self.speculative(request, workspace)
        state = gate._state
        lock = state.locks.lock(task_id)
        async with lock:
            checkpoint_id = state.checkpoint_by_task.get(task_id)
            if checkpoint_id is None:
                checkpoint = await gate._checkpoints.capture(
                    task_id=task_id,
                    workspace_root=workspace.root,
                    label=f"transactional:{task_id}",
                )
                checkpoint_id = checkpoint["id"]
                state.checkpoint_by_task[task_id] = checkpoint_id
                state.checkpoint_root_by_task[task_id] = workspace.root
                base_fingerprint = str(checkpoint.get("workspace_fingerprint") or "")
                manifest_result = await run_checkpoint_worker(
                    "manifest", root=workspace.root, workspace_root=workspace.root
                )
                state.transaction_records[task_id] = {
                    "transaction_id": task_id,
                    "checkpoint_id": checkpoint_id,
                    "workspace_root": workspace.root,
                    "base_fingerprint": base_fingerprint,
                    "last_owned_fingerprint": base_fingerprint,
                    "base_manifest": dict(manifest_result.get("manifest") or {}),
                    "postconditions": planned_postconditions(request, workspace.root),
                    "resources": transaction_resources(request, workspace.root),
                    "mutation_ids": [],
                    "started_at": utcnow().isoformat(),
                    "state": "ACTIVE",
                }
                state.persist()
        return RealityRoute(
            workspace, ExecutionDisposition.TRANSACTIONAL, checkpoint_id=checkpoint_id
        )

    async def speculative(
        self,
        request: CapabilityRequest,
        workspace: WorkspaceSpec,
    ) -> RealityRoute:
        """Lazily open the task's sticky candidate branch."""
        gate = self._gate
        task_id = request.task_id
        if task_id is None:
            return RealityRoute(workspace, ExecutionDisposition.DIRECT)
        lock = gate._state.locks.lock(task_id)
        async with lock:
            branch = gate.active_branch(task_id)
            if branch is None:
                branch = await gate._shadow.open_branch(
                    task_id=task_id,
                    base_workspace=workspace,
                    proposal=[],
                )
                gate.activate_branch(branch)
            target = branch.shadow_workspace
            translate_workspace_arguments(request, workspace.root, target.root)
        return RealityRoute(target, ExecutionDisposition.SPECULATIVE, transaction_id=branch.id)


async def route_request(
    gate: Any,
    request: CapabilityRequest,
    workspace: WorkspaceSpec,
    effects: Mapping[EffectClass, Any]
    | frozenset[EffectClass]
    | tuple[EffectClass, ...]
    | set[EffectClass],
    descriptor: CapabilityDescriptor,
    tier: str | None = None,
) -> RealityRoute:
    """Resolve the execution workspace for a concrete request."""
    from athena.reality.gate import _mutation_mode, _same_root

    mode = _mutation_mode(workspace.mutation_mode)
    request_origin = getattr(request.origin, "value", request.origin)
    if request_origin == "system_verification" or bool(
        (request.metadata or {}).get("_verified_candidate_commit")
    ):
        # Verification is already bound by the canonical verifier to the
        # candidate workspace it was given, and a verified commit request is
        # bound to the real base workspace selected by the shadow commit
        # controller. Treating either as a fresh model mutation re-opened a
        # nested speculative branch and made the later commit look like
        # workspace drift.
        return RealityRoute(workspace, ExecutionDisposition.DIRECT)
    if (
        request.task_id
        and gate._dispatcher_runtime_escalations is not None
        and request.task_id in gate._dispatcher_runtime_escalations
    ):
        mode = MutationMode.SPECULATIVE

    active = gate.active_branch(request.task_id) if request.task_id else None
    if active is not None and is_workspace_bound(request, descriptor):
        if _same_root(workspace.root, active.shadow_workspace.root):
            return RealityRoute(
                active.shadow_workspace,
                ExecutionDisposition.SPECULATIVE,
                transaction_id=active.id,
            )
        if not _same_root(workspace.root, active.base_workspace.root):
            raise PermissionError(
                "task workspace changed while a speculative transaction is active"
            )
        translate_workspace_arguments(request, workspace.root, active.shadow_workspace.root)
        return RealityRoute(
            active.shadow_workspace,
            ExecutionDisposition.SPECULATIVE,
            transaction_id=active.id,
        )

    task_id = request.task_id
    state = gate._state
    transaction_record = state.transaction_records.get(task_id) if task_id else None
    if transaction_record is not None and transaction_record.get("state") == "RECOVERY_REQUIRED":
        raise PermissionError(
            "transaction requires operator recovery before more workspace operations"
        )
    if transaction_record is not None and transaction_record.get("state") == "COMMIT_PROVEN":
        raise PermissionError("transaction completion is pending durable task finalization")
    checkpoint_id = state.checkpoint_by_task.get(task_id) if task_id else None
    if checkpoint_id is not None and is_workspace_bound(request, descriptor):
        assert task_id is not None
        checkpoint_root = state.checkpoint_root_by_task.get(task_id)
        if checkpoint_root is not None and not _same_root(workspace.root, checkpoint_root):
            raise PermissionError(
                "task workspace changed while a transactional checkpoint is active"
            )
        return RealityRoute(
            workspace,
            ExecutionDisposition.TRANSACTIONAL,
            checkpoint_id=checkpoint_id,
        )

    if mode is MutationMode.DIRECT:
        if not is_opaque_execution(request, effects, descriptor):
            return RealityRoute(workspace, ExecutionDisposition.DIRECT)
    sensitive = is_project_sensitive(request, effects, descriptor)
    if not sensitive:
        return RealityRoute(workspace, ExecutionDisposition.DIRECT)
    if mode is MutationMode.READ_ONLY:
        raise PermissionError(
            f"project mutation denied by read-only workspace: {request.capability_id}"
        )
    if request.task_id is None:
        raise PermissionError("speculative project mutations require a task-scoped invocation")
    classification = gate.classify(
        request,
        tier,
        effects,
        descriptor,
        workspace=replace(workspace, mutation_mode=mode),
    )
    disposition = classification.disposition
    if disposition is ExecutionDisposition.ISOLATED:
        route = await gate._routing.isolated(request, workspace, descriptor)
        return RealityRoute(
            route.workspace,
            route.disposition,
            route.transaction_id,
            route.checkpoint_id,
            classification,
        )
    if disposition is ExecutionDisposition.TRANSACTIONAL:
        route = await gate._routing.transactional(request, workspace)
        return RealityRoute(
            route.workspace,
            route.disposition,
            route.transaction_id,
            route.checkpoint_id,
            classification,
        )
    if disposition is ExecutionDisposition.DIRECT:
        return RealityRoute(workspace, ExecutionDisposition.DIRECT, classification=classification)
    route = await gate._routing.speculative(request, workspace)
    return RealityRoute(
        route.workspace,
        route.disposition,
        route.transaction_id,
        route.checkpoint_id,
        classification,
    )


def planned_postconditions(request: CapabilityRequest, workspace_root: str) -> dict[str, str]:
    """Describe a directly requested filesystem post-state before dispatch."""
    if request.capability_id != "fs":
        return {}
    args = request.arguments or {}
    operation = str(args.get("operation") or "").casefold()
    raw_path = args.get("path")
    if not isinstance(raw_path, str) or operation not in {"write", "patch", "delete"}:
        return {}
    path = raw_path if os.path.isabs(raw_path) else os.path.join(workspace_root, raw_path)
    path = os.path.realpath(os.path.abspath(path))
    if operation == "delete":
        return {path: "<missing>"}
    content = args.get("content") if operation == "write" else args.get("new_content")
    if content is None:
        encoded = (
            args.get("content_base64") if operation == "write" else args.get("new_content_base64")
        )
        if isinstance(encoded, str):
            try:
                payload = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                return {}
        else:
            return {}
    elif isinstance(content, str):
        payload = content.encode("utf-8")
    else:
        return {}
    return {path: hashlib.sha256(payload).hexdigest()}


def transaction_resources(request: CapabilityRequest, workspace_root: str) -> list[str]:
    """Record the workspace resources named by a transaction request."""
    args = request.arguments or {}
    resources: list[str] = []
    for key in ("path", "destination", "cwd", "workdir"):
        value = args.get(key)
        if not isinstance(value, str):
            continue
        path = value if os.path.isabs(value) else os.path.join(workspace_root, value)
        resources.append(os.path.realpath(os.path.abspath(path)))
    return list(dict.fromkeys(resources))
