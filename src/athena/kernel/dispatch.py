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

from dataclasses import dataclass

from athena.capabilities.dispatcher import (
    CapabilityDispatcher,
    SuspendedCall,
    WAITING_APPROVAL,
)
from athena.protocol.capabilities import (
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
)
from athena.protocol.messages import (
    CapabilityCallBlock,
    CapabilityResultBlock,
)
from athena.protocol.tasks import TaskSpec, WorkspaceSpec

__all__ = [
    "CapabilityDispatchShim",
    "DispatchResult",
    "SuspendedCall",
    "WAITING_APPROVAL",
]


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
        self._workspace = workspace
        self._profile = profile

    async def dispatch(self, task: TaskSpec, calls) -> DispatchResult:
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
        workspace = task.workspace or self._workspace

        outcome = await self._dispatcher.dispatch_many(
            requests,
            workspace=workspace,
            profile=self._profile,
            task_policy=task.capability_policy,
            task_budget=task.resource_budget,
        )

        results: list[CapabilityResultBlock] = []
        suspended: list[SuspendedCall] = []
        for item in outcome:
            if isinstance(item, SuspendedCall):
                suspended.append(item)
            else:
                results.append(_result_to_block(item))

        return DispatchResult(results=tuple(results), suspended=tuple(suspended))


def _to_request(task: TaskSpec, call: CapabilityCallBlock) -> CapabilityRequest:
    return CapabilityRequest(
        capability_id=call.capability_id,
        arguments=dict(call.arguments or {}),
        task_id=task.id,
        session_id=task.session_id,
        call_id=call.call_id,
        candidate=call.candidate,
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
