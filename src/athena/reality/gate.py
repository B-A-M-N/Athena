"""The execution reality boundary.

The capability name is not a sufficient safety boundary: ``execute``, a
generated capability, or a workflow can mutate a project without calling
``fs``.  ``RealityGate`` therefore classifies the concrete request at the
dispatcher boundary and binds every project-sensitive operation to one of
four dispositions:

    DIRECT        - the caller opted into immediate, unrecoverable mutation of
                    the real workspace.  No bookkeeping.
    ISOLATED      - a single, independently-discardable shadow clone for one
                    call.  The call observes one coherent copy-on-write reality
                    but never joins the task's sticky candidate branch.
    TRANSACTIONAL - the real workspace is mutated in place, but a checkpoint of
                    the workspace is captured first so a later failure can roll
                    the project back to exactly the pre-operation revision.
    SPECULATIVE   - a task-local, sticky shadow branch that accumulates every
                    project-sensitive operation until the task proves and
                    promotes (or discards) the whole candidate.

The gate is deliberately deterministic and has no model-facing decision.
Selection is driven by an explicit ``tier`` (forced by the caller) when
present, otherwise a conservative default heuristic:

    not project-sensitive            -> DIRECT
    sensitive, already on a branch    -> SPECULATIVE (coherent candidate)
    sensitive, single reversible op   -> ISOLATED
    sensitive, forced transactional    -> TRANSACTIONAL
    sensitive, otherwise              -> SPECULATIVE

This is the escalation ladder the dynamic speculation design points at: the
smallest safe change occupies the cheapest tier, and only a change whose blast
radius actually requires a full candidate promotes to SPECULATIVE.
"""

from __future__ import annotations

import os
from typing import Any, Mapping

from athena.protocol.capabilities import CapabilityDescriptor, CapabilityRequest, EffectClass
from athena.protocol.reality import ExecutionDisposition
from athena.protocol.tasks import MutationMode, WorkspaceSpec
from athena.reality.classification import RealityClassification
from athena.reality.classification_operations import RealityRequestClassifier
from athena.reality.branch_registry import RealityBranchRegistry
from athena.reality.routing import RealityRoute
from athena.reality.routing_operations import RealityRouting
from athena.reality.state import (
    RealityRouteState,
    resolve_state_root,
)
from athena.reality.transaction_operations import (
    RealityTransactionOperations,
    TransactionRecoveryRequired,
)


class RealityGate:
    """Bind speculative / isolated / transactional task actions to reality."""

    _dispatcher: Any | None = None
    _dispatcher_runtime_escalations: set[str] | None = None

    def bind_dispatcher(self, dispatcher) -> None:
        """Attach the dispatcher whose runtime escalation set this gate reads.

        Keep a narrow identity backreference for verified late-complexity
        recording; it is not an execution or dispatch authority. The dispatcher
        records admission-independent complex operation sets; RealityGate
        remains the sole authority that creates/retains candidate workspaces
        and translates request targets.
        """
        self._dispatcher = dispatcher
        self._dispatcher_runtime_escalations = getattr(
            dispatcher, "_runtime_speculative_tasks", None
        )

    def __init__(self, shadow_engine, *, checkpoint_manager=None) -> None:
        self._shadow = shadow_engine
        self._checkpoints = checkpoint_manager
        self._branches = RealityBranchRegistry(shadow_engine)
        self._state_root = resolve_state_root(shadow_engine)
        self._state = RealityRouteState(self._state_root)
        self._request_classifier = RealityRequestClassifier()
        self._routing = RealityRouting(self)
        self._transactions = RealityTransactionOperations(self)
        self._state.load()
        self._rehydrate_active_branches()

    @property
    def locks(self):
        """Expose route-lock coordination without exposing mutable state maps."""
        return self._state.locks

    def transaction_record(self, task_id: str | None) -> Mapping[str, Any] | None:
        """Return one transaction record for completion projections."""
        if task_id is None:
            return None
        return self._state.transaction_records.get(task_id)

    def bind_checkpoint_manager(self, checkpoint_manager) -> None:
        """Attach the causal checkpoint backend used by TRANSACTIONAL tiers."""
        self._checkpoints = checkpoint_manager

    def active_branch(self, task_id: str | None):
        """Return the task's sticky candidate branch, if one exists."""
        return self._branches.active_branch(task_id)

    def activate_branch(self, branch: Any) -> None:
        """Retain a verified candidate for operator review after proof."""
        self._branches.activate(branch)

    def active_branches(self) -> tuple[Any, ...]:
        return self._branches.active_branches()

    def ephemeral_branch(self, call_id: str | None):
        """Return a per-call isolated branch, if one was opened for it."""
        return self._branches.ephemeral_branch(call_id)

    def checkpoint_id(self, task_id: str | None) -> str | None:
        """Return the task's transactional checkpoint id, if any."""
        return self._state.checkpoint_by_task.get(task_id) if task_id else None

    def transaction_fingerprint(self, task_id: str | None) -> str | None:
        """Return the exact post-state last produced by a transaction."""
        if task_id is None:
            return None
        value = self._state.transaction_records.get(task_id, {}).get("last_owned_fingerprint")
        return str(value) if value else None

    def mark_transaction_recovery_required(
        self,
        task_id: str | None,
        *,
        error: str,
    ) -> None:
        self._transactions.mark_recovery_required(task_id, error=error)

    def _rehydrate_active_branches(self) -> None:
        """Reattach durable candidate branches after process restart.

        A branch that was already committing is deliberately not reattached:
        startup reconciliation must first classify that uncertain commit as
        recovery-required.  Reattaching it here could route fresh work into a
        branch whose real-world outcome is unknown.
        """
        self._branches.rehydrate()

    async def route(
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
        return await self._routing.route_request(
            request,
            workspace,
            effects,
            descriptor,
            tier=tier,
        )

    def classify(
        self,
        request: CapabilityRequest,
        tier: str | None,
        effects: Any,
        descriptor: CapabilityDescriptor,
        *,
        workspace: WorkspaceSpec | None = None,
    ) -> RealityClassification:
        """Classify a concrete request using only deterministic facts."""
        return self._request_classifier.classify(
            request,
            tier,
            effects,
            descriptor,
            workspace=workspace,
            checkpoint_available=self._checkpoints is not None,
        )

    async def discard_ephemeral(self, call_id: str | None) -> None:
        """Drop a per-call isolated branch if one was opened for it."""
        await self._branches.discard_ephemeral(call_id)

    async def deactivate_branch(self, task_id: str | None) -> None:
        """Clear the task's active branch from the gate's routing map.

        Called after the shadow engine has committed or discarded the branch
        so subsequent operations don't route to a stale workspace.
        """
        self._branches.deactivate(task_id)

    async def compensate(self, task_id: str | None) -> bool:
        """Roll a transactional task back to its pre-mutation checkpoint.

        Returns True if a checkpoint existed and was restored, False if there
        was nothing to roll back.  Only the exact captured revision is
        restored; concurrent changes are refused by the checkpoint manager.
        """
        return await self._transactions.compensate(task_id)

    async def note_transaction_progress(
        self,
        task_id: str | None,
        workspace_root: str,
        *,
        mutation: bool,
        mutation_id: str | None = None,
        resource: str | None = None,
    ) -> None:
        await self._transactions.note_progress(
            task_id,
            workspace_root,
            mutation=mutation,
            mutation_id=mutation_id,
            resource=resource,
        )

    def mark_transaction_proven(self, task_id: str | None) -> None:
        self._transactions.mark_proven(task_id)

    async def reconcile_startup(self) -> int:
        """Reconcile durable transaction ownership before new work routes.

        An ACTIVE transaction is safe to resume only when the workspace still
        equals the exact revision last owned by the transaction.  A crash
        between the filesystem effect and ``note_transaction_progress`` (or
        an external edit) therefore becomes an explicit recovery boundary
        instead of an implicit authorization to keep mutating.
        """
        return await self._transactions.reconcile_startup()

    async def finalize_transaction(self, task_id: str | None) -> None:
        await self._transactions.finalize(task_id)

    def _base_root(self, task_id: str) -> str:
        # The transactional tier mutated the real workspace in place; use the
        # root recorded when its checkpoint was captured.  The speculative
        # branch's base is the only other known root.
        stored = self._state.checkpoint_root_by_task.get(task_id)
        if stored is not None:
            return stored
        branch = self._branches.active_branch(task_id)
        if branch is not None:
            return branch.base_workspace.root
        return os.getcwd()


def _mutation_mode(value: MutationMode | str | None) -> MutationMode:
    if isinstance(value, MutationMode):
        return value
    try:
        return MutationMode(str(value or MutationMode.DIRECT.value))
    except ValueError:
        return MutationMode.DIRECT


def _same_root(first: str, second: str) -> bool:
    return os.path.realpath(os.path.abspath(first)) == os.path.realpath(os.path.abspath(second))


__all__ = ["ExecutionDisposition", "RealityGate", "TransactionRecoveryRequired"]
