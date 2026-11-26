"""Capability dispatch shim for AgentKernel (BUILDSPEC §18, INV-004).

The kernel owns *requesting* capability dispatch but must not know capability
internals. This module translates model-requested ``CapabilityCallBlock``
objects into ``CapabilityRequest`` objects and forwards them to the single
capability path (``CapabilityDispatcher``). The dispatcher itself lives in
``athena.capabilities``; everything here is a thin, provider-neutral shim.

Only this shim talks to the capability layer. No model block can bypass it
(INV-004).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from athena.protocol.capabilities import DispatchProvenance
from athena.capabilities.dispatcher import CapabilityDispatcher
from athena.protocol.capabilities import (
    CapabilityRequest,
    CapabilityRequestOrigin,
    CapabilityResult,
    CapabilityResultStatus,
    DispatchDirectives,
)
from athena.protocol.continuations import SuspendedCall
from athena.protocol.messages import (
    CapabilityCallBlock,
    CapabilityResultBlock,
)
from athena.protocol.tasks import TaskSpec, WorkspaceSpec

__all__ = [
    "CapabilityDispatchShim",
    "DispatchScope",
    "DispatchResult",
    "SuspendedCall",
    "WAITING_APPROVAL",
]

WAITING_APPROVAL = "WAITING_APPROVAL"


@dataclass(frozen=True)
class DispatchResult:
    """Aggregated outcome of a capability dispatch round for one task turn.

    ``results`` are per-call capability result *blocks* (provider-neutral
    ``CapabilityResultBlock``), ready to be stored in the session transcript.
    ``suspended`` is non-empty when at least one call was parked on an ``ask``
    policy decision and the task must enter WAITING_APPROVAL before further
    reasoning happens.
    """

    results: tuple[CapabilityResultBlock, ...] = ()
    suspended: tuple[SuspendedCall, ...] = ()

    @property
    def has_suspension(self) -> bool:
        return bool(self.suspended)


@dataclass(frozen=True)
class DispatchScope:
    """Public, immutable context for the task's canonical dispatch boundary."""

    workspace: WorkspaceSpec
    profile: str | None


class CapabilityDispatchShim:
    """Facade turning model blocks into authorized capability execution.

    Constructor dependencies:

    * ``dispatcher`` — the ``CapabilityDispatcher`` (single capability path).
    * ``workspace`` — the active ``WorkspaceSpec`` used by policy scoping.
    """

    def __init__(
        self,
        dispatcher: CapabilityDispatcher,
        workspace: WorkspaceSpec,
        *,
        profile: str | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self.scope = DispatchScope(workspace=workspace, profile=profile)

    async def dispatch(
        self,
        task: TaskSpec,
        calls,
        *,
        runtime_remaining_s: float | None = None,
        provenance: "DispatchProvenance | None" = None,
    ) -> DispatchResult:
        """Dispatch all capability calls for one assistant turn.

        Every call is translated into a ``CapabilityRequest`` and run through
        ``CapabilityDispatcher.dispatch_many`` (parallel by default). Calls that
        come back as ``SuspendedCall`` (WAITING_APPROVAL) are accumulated
        separately so the kernel can suspend the task.
        """
        calls = list(calls)
        if not calls:
            return DispatchResult()

        requests = [_to_request(task, call) for call in calls]
        workspace = getattr(task, "workspace", None) or self.scope.workspace

        outcome = await self._dispatcher.dispatch_many(
            requests,
            workspace=workspace,
            profile=self.scope.profile,
            task_policy=task.capability_policy,
            model_policy=task.model_policy,
            task_budget=task.resource_budget,
            task_deadline=task.deadline,
            runtime_remaining_s=runtime_remaining_s,
            provenance=provenance,
        )

        results: list[CapabilityResultBlock] = []
        suspended: list[SuspendedCall] = []
        for item in outcome:
            if isinstance(item, SuspendedCall):
                suspended.append(item)
            else:
                results.append(_result_to_block(item))

        return DispatchResult(results=tuple(results), suspended=tuple(suspended))

    async def resume_requests(
        self,
        task: TaskSpec,
        requests,
        *,
        runtime_remaining_s: float | None = None,
        provenance: "DispatchProvenance | None" = None,
        task_policy=None,
        model_policy=None,
        task_budget=None,
        task_deadline=None,
        _directives_by_call_id: dict | None = None,
        verification_environment: Any = None,
    ) -> DispatchResult:
        """Resume canonical requests with explicit replay authority.

        Approvals bind canonical requests, not model blocks.  Trusted
        orchestration resumes them through the same shim boundary, preserving
        task policy and directives without exposing dispatcher internals.
        """
        requests = list(requests)
        if not requests:
            return DispatchResult()

        workspace = getattr(task, "workspace", None) or self.scope.workspace
        outcome = await self._dispatcher.dispatch_many(
            requests,
            workspace=workspace,
            profile=self.scope.profile,
            task_policy=task_policy
            if task_policy is not None
            else getattr(task, "capability_policy", None),
            model_policy=model_policy
            if model_policy is not None
            else getattr(task, "model_policy", None),
            task_budget=task_budget
            if task_budget is not None
            else getattr(task, "resource_budget", None),
            task_deadline=task_deadline
            if task_deadline is not None
            else getattr(task, "deadline", None),
            runtime_remaining_s=runtime_remaining_s,
            provenance=provenance,
            directives_by_call_id=_directives_by_call_id or {},
            verification_environment=verification_environment,
        )

        results: list[CapabilityResultBlock] = []
        suspended: list[SuspendedCall] = []
        for item in outcome:
            if isinstance(item, SuspendedCall):
                suspended.append(item)
            else:
                results.append(_result_to_block(item))
        return DispatchResult(results=tuple(results), suspended=tuple(suspended))

    async def resume_request(
        self,
        task: TaskSpec,
        request: CapabilityRequest,
        *,
        directives: "DispatchDirectives | None" = None,
        provenance: "DispatchProvenance | None" = None,
        **replay_context,
    ) -> "CapabilityResult | SuspendedCall":
        """Resume one approved canonical request without model-block repair."""
        request = replace(
            request,
            origin=CapabilityRequestOrigin.TRUSTED_ORCHESTRATION,
            metadata=dict(request.metadata or {}),
        )
        outcome = await self.resume_requests(
            task,
            [request],
            provenance=provenance,
            _directives_by_call_id={request.call_id: directives} if directives else None,
            **replay_context,
        )
        if outcome.results:
            return _result_from_block(outcome.results[0])
        if outcome.suspended:
            return outcome.suspended[0]
        raise RuntimeError("resume_request returned no result")

    def resolve_effects(self, request: CapabilityRequest, workspace: WorkspaceSpec):
        """Resolve declared effects without authorization or execution."""
        return self._dispatcher.resolve_effects(request, workspace)


def _result_from_block(block: CapabilityResultBlock) -> CapabilityResult:
    return CapabilityResult(
        block.call_id,
        block.capability_id,
        CapabilityResultStatus.OK if block.ok else CapabilityResultStatus.FAILED,
        output=block.output,
        error=block.error,
        metadata=dict(block.metadata or {}),
        ref_uri=block.ref_uri,
    )


def _to_request(task: TaskSpec, call: CapabilityCallBlock) -> CapabilityRequest:
    return CapabilityRequest(
        capability_id=call.capability_id,
        arguments=dict(call.arguments or {}),
        task_id=task.id,
        session_id=task.session_id,
        call_id=call.call_id,
        candidate=call.candidate,
        metadata=dict(task.metadata or {}),
    )


def _result_to_block(result: CapabilityResult) -> CapabilityResultBlock:
    if isinstance(result, CapabilityResultBlock):
        return result
    return CapabilityResultBlock(
        call_id=getattr(result, "call_id", ""),
        capability_id=getattr(result, "capability_id", ""),
        ok=(getattr(result, "status", None) is CapabilityResultStatus.OK),
        output=getattr(result, "output", "") or "",
        error=getattr(result, "error", None),
        metadata=getattr(result, "metadata", None) or {},
        ref_uri=getattr(result, "ref_uri", None),
    )
