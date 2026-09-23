"""CapabilityDispatcher internal mechanisms (P1-10).

Extracted from CapabilityDispatcher as subordinate mechanisms, not second
authorities: exactly ONE canonical ``dispatch()`` entrypoint remains on the
dispatcher, and every store, policy, fabric, lock table, budget, and cache
resolves through the owning dispatcher instance (``self._d``). These modules
hold HOW a call is ordered, repaired, policy-gated, and evidence-cached —
never WHETHER dispatch happens.

- :class:`DispatchOrdering` — resource locks, batch ordering lanes, budget
  leases around the dispatch call.
- :class:`DispatchRepair` — preflight validation/repair of whole batches and
  durable repair receipts.
- :class:`PolicyGate` — effect resolution, task-policy ceilings, and approval
  parking (durable ApprovalStore + in-memory manager + continuations).
- :class:`ResultCache` — health persistence, result observations, failure
  memory, and the bounded result cache lookup/invalidation.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Mapping

from athena.policy.approvals import args_digest
from athena.protocol.capabilities import (
    CapabilityRequest,
    CapabilityRequestOrigin,
    CapabilityResult,
    ExternalEffectPhase,
    CapabilityResultStatus,
    DispatchDirectives,
    DispatchProvenance,
    EffectClass,
)
from athena.protocol.errors import CapabilityUnavailable, PersistenceError
from athena.protocol.ids import new_id
from athena.protocol.events import EV
from athena.protocol.reality import ExecutionDisposition
from athena.protocol.policy import (
    ApprovalScope,
    PolicyDecision,
    PolicyRequest,
    PolicyVerdict,
)
from athena.protocol.tasks import CapabilityPolicy, capability_id_permitted
from athena.schema import validate_schema
from athena.protocol.tasks import ResourceBudget, WorkspaceSpec

if TYPE_CHECKING:
    from athena.capabilities.dispatcher import CapabilityDispatcher

from athena.capabilities.prepared import PreparationFailure, PreparedCapabilityCall
from athena.capabilities.dispatch_helpers import (
    HIGH_RISK_EFFECTS,
    ReferenceCountedKeyedLocks,
    is_execution as _is_execution,
    is_ordering_sensitive as _is_ordering_sensitive,
    primary_effect as _primary_effect,
    resource_key as _resource_key,
    _STRICTNESS,
)

__all__ = ["DispatchOrdering", "DispatchRepair", "PolicyGate", "DispatchProvenance"]

_logger = logging.getLogger("athena.capabilities")


def _high_risk_effects():
    return HIGH_RISK_EFFECTS


class _RealityBoundaryDenied(Exception):
    """Internal: reality routing refused; carries the canonical result."""

    def __init__(self, result: "CapabilityResult") -> None:
        super().__init__(result.error or "reality boundary denied")
        self.result = result


class _Mechanism:
    def __init__(self, dispatcher: "CapabilityDispatcher") -> None:
        self._d = dispatcher


class DispatchOrdering(_Mechanism):
    """Resource locking and batch-order lanes around dispatch."""

    def _batch_order(
        self,
        request: CapabilityRequest,
        workspace: WorkspaceSpec,
        effects: tuple[EffectClass, ...],
        batch_order_lock: asyncio.Lock | None,
        *,
        directives: DispatchDirectives | None = None,
        descriptor: Any | None = None,
        executor: Any | None = None,
    ) -> list[tuple[asyncio.Lock, ReferenceCountedKeyedLocks]]:
        """Ordering for one call, or [] when it may parallelize.

        Resource-ambiguous dependency-bearing calls — a write/delete/child/
        external with no concrete named resource — cannot prove independence
        from siblings mutating ambient state, so they serialize on a
        dispatcher-owned lane keyed by the workspace/reality boundary and the
        governed resource class (item 5). Two concurrent batches therefore
        share one lane instead of each building an unrelated per-batch lock.
        Named-path calls (already per-resource-locked) and pure reads bypass
        the lane.  Workflow run/trial calls use a separate envelope lane:
        their implementation awaits child dispatches, and holding the child
        operation lane across that await is not reentrant.
        """
        if getattr(executor, "mediates_nested_dispatch", False):
            return []
        if not _is_ordering_sensitive(effects):
            return []
        # A trusted reality route has already selected an isolated execution
        # domain.  Keeping it on the workspace-wide ambient lane would erase
        # that isolation's concurrency benefit and recreate head-of-line
        # blocking between unrelated operations.
        if directives is not None and directives.reality_tier == "isolated":
            return []
        reality_gate = self._d._reality_gate
        if (
            reality_gate is not None
            and descriptor is not None
            and not (
                request.task_id
                and (
                    reality_gate.active_branch(request.task_id) is not None
                    or reality_gate.checkpoint_id(request.task_id) is not None
                )
            )
        ):
            classification = reality_gate.classify(
                request,
                directives.reality_tier if directives is not None else None,
                effects,
                descriptor,
                workspace=workspace,
            )
            if classification.disposition is ExecutionDisposition.ISOLATED:
                return []
        if self.resource_keys_for(request, workspace, effects):
            return []
        boundary = workspace.id or workspace.root
        arguments = request.arguments or {}
        if request.capability_id == "workflow" and arguments.get("operation") in {
            "run",
            "trial",
        }:
            envelope_identity = (
                arguments.get("run_id")
                or request.call_id
                or arguments.get("workflow_id")
            )
            if envelope_identity:
                envelope_key = (boundary, "workflow-envelope", str(envelope_identity))
                return [
                    (
                        self._d._workflow_lanes.acquire_reference(envelope_key),
                        self._d._workflow_lanes,
                    )
                ]
        lane_key = (boundary, "ambient-order-lane")
        return [
            (
                self._d._order_lanes.acquire_reference(lane_key),
                self._d._order_lanes,
            )
        ]

    async def _dispatch_with_controls(
        self,
        prepared: CapabilityRequest | PreparedCapabilityCall,
        *,
        workspace: WorkspaceSpec | None = None,
        profile: str | None = None,
        task_policy: CapabilityPolicy | None = None,
        model_policy: Any = None,
        task_budget: ResourceBudget | None = None,
        task_deadline: datetime | None = None,
        runtime_remaining_s: float | None = None,
        verification_environment: Any = None,
        directives: DispatchDirectives | None = None,
        batch_order_lock: asyncio.Lock | None = None,
        provenance: DispatchProvenance | None = None,
        **kwargs,
    ):
        """Apply task concurrency and resource conflict controls.

        A fully prepared call is consumed as-is. A raw request is resolved
        once here for the direct/resume path. The capability itself remains
        the authority for effects; controls only use the declared contract to
        bound concurrency and prevent same-resource races.
        """
        if isinstance(prepared, PreparedCapabilityCall):
            request = prepared.request
            workspace = prepared.workspace
            effects = prepared.effects
            if prepared.executor is None:
                # Preflight failures never enter controls; retain fail-closed.
                raise ValueError("cannot dispatch an unprepared capability call")
        else:
            request = prepared
            if workspace is None:
                raise ValueError("workspace is required for an unprepared request")
            try:
                prepared = await self._d.prepare_one(
                    request,
                    workspace,
                    provenance=provenance,
                    directives=directives,
                )
            except (CapabilityUnavailable, KeyError, TypeError, ValueError) as exc:
                return CapabilityResult(
                    request.call_id or new_id("call"),
                    request.capability_id,
                    CapabilityResultStatus.FAILED,
                    error=str(exc) or "unknown-capability",
                )
            request = prepared.request
            effects = prepared.effects

        effective_directives = (
            prepared.directives
            if isinstance(prepared, PreparedCapabilityCall) and prepared.directives is not None
            else directives
        )
        descriptor = (
            prepared.descriptor
            if isinstance(prepared, PreparedCapabilityCall)
            else getattr(prepared.executor, "descriptor", None)
        )

        # asyncio.Lock is not reentrant: a mediated nested dispatch would
        # deadlock against its own outer lock, so track ownership per task and
        # skip locks this task already holds.
        current = asyncio.current_task()
        if current is None:  # pragma: no cover - dispatch always runs in a task
            raise RuntimeError("capability dispatch requires a running asyncio task")
        held = self._d._task_held_locks.setdefault(current, set())

        executor = getattr(prepared, "executor", None)
        resource_keys = () if getattr(executor, "mediates_nested_dispatch", False) else (
            prepared.resource_keys
            if isinstance(prepared, PreparedCapabilityCall)
            else self.resource_keys_for(request, workspace, effects, executor=executor)
        )

        async def invoke_with_locks():
            all_locks = self._locks_for_keys(resource_keys, workspace)
            locks = [lock for lock in all_locks if lock not in held]
            for lock in all_locks:
                if lock in held:
                    self._d._resource_locks.release_reference(lock)
            order = self._d._batch_order(
                request,
                workspace,
                effects,
                batch_order_lock,
                directives=effective_directives,
                descriptor=descriptor,
                executor=executor,
            )
            # Reservation is the ownership boundary, not lock state.  Acquire
            # all ambient and resource locks as one transaction so cancellation
            # cannot strand the ordering lane or a half-acquired resource set.
            reserved = [
                *order,
                *((lock, self._d._resource_locks) for lock in locks),
            ]
            acquired: list[tuple[asyncio.Lock, ReferenceCountedKeyedLocks]] = []
            try:
                for lock, table in reserved:
                    await lock.acquire()
                    acquired.append((lock, table))
            except BaseException:
                # Only a completed ``await lock.acquire()`` establishes
                # ownership.  ``locked()`` says nothing about which task owns
                # the lock, so it must never be used to infer cancellation
                # cleanup ownership here.
                for released_lock, table in reversed(acquired):
                    released_lock.release()
                    table.release_reference(released_lock)
                outstanding = list(reserved)
                for acquired_entry in acquired:
                    outstanding.remove(acquired_entry)
                for reserved_lock, table in outstanding:
                    table.release_reference(reserved_lock)
                raise
            for lock, _table in acquired:
                held.add(lock)
            try:
                return await self._d._dispatch_prepared_core(
                    prepared,
                    workspace=workspace,
                    profile=profile,
                    task_policy=task_policy,
                    model_policy=model_policy,
                    task_budget=task_budget,
                    task_deadline=task_deadline,
                    runtime_remaining_s=runtime_remaining_s,
                    verification_environment=verification_environment,
                    provenance=provenance,
                )
            finally:
                for lock in locks:
                    held.discard(lock)
                self._d._resource_locks.release_many(locks)
                if not held:
                    self._d._task_held_locks.pop(current, None)
                for lane_lock, lane_table in reversed(order):
                    lane_lock.release()
                    lane_table.release_reference(lane_lock)

        lease = None
        if request.task_id and _is_execution(effects):
            if self._d._budgets is not None:
                lease = self._d._budgets.execution_lease(request.task_id)
            elif task_budget is not None:
                # Compatibility for standalone dispatcher users that have not
                # composed a BudgetTracker yet.  Service wiring always binds
                # the hierarchical authority above.
                semaphore = self._d._execution_semaphores.get(request.task_id)
                if semaphore is None:
                    semaphore = asyncio.Semaphore(max(1, int(task_budget.max_parallel_executions)))
                    self._d._execution_semaphores[request.task_id] = semaphore
                await semaphore.acquire()
                try:
                    return await invoke_with_locks()
                finally:
                    semaphore.release()
        if lease is None:
            return await invoke_with_locks()
        async with lease:
            return await invoke_with_locks()

    def resource_keys_for(
        self,
        request: CapabilityRequest,
        workspace: WorkspaceSpec,
        effects: tuple[EffectClass, ...],
        *,
        executor=None,
    ) -> tuple[str, ...]:
        """Resolve normalized resource identities without taking a lock."""
        if not set(effects) & {
            EffectClass.READ_LOCAL,
            EffectClass.WRITE_LOCAL,
            EffectClass.DELETE,
        }:
            return ()
        args = request.arguments or {}
        if executor is None:
            try:
                executor = self._d._executor_for(request, workspace)
            except (CapabilityUnavailable, KeyError, TypeError, ValueError):
                return ()
        descriptor = getattr(executor, "descriptor", None)
        resolver = getattr(descriptor, "resource_key_resolver", None)
        if resolver is not None:
            try:
                resolved = tuple(resolver(args, workspace))
            except (OSError, TypeError, ValueError):
                return ()
            return tuple(sorted({key for key in resolved if isinstance(key, str) and key}))
        raw_resources = [args.get("path"), args.get("destination")]
        return tuple(
            sorted(
                {
                    _resource_key(workspace, value)
                    for value in raw_resources
                    if isinstance(value, str) and value
                }
            )
        )

    def _locks_for_request(
        self,
        request: CapabilityRequest,
        workspace: WorkspaceSpec,
        effects: tuple[EffectClass, ...],
        *,
        executor=None,
    ) -> list[asyncio.Lock]:
        resources = self.resource_keys_for(request, workspace, effects, executor=executor)
        if not resources:
            return []
        locks: list[asyncio.Lock] = []
        for resource in sorted(resources):
            key = (workspace.id or workspace.root, resource)
            locks.append(self._d._resource_locks.acquire_reference(key))
        return locks

    def _locks_for_keys(
        self,
        resource_keys: tuple[str, ...],
        workspace: WorkspaceSpec,
    ) -> list[asyncio.Lock]:
        if not resource_keys:
            return []
        boundary = workspace.id or workspace.root
        return [
            self._d._resource_locks.acquire_reference((boundary, resource))
            for resource in resource_keys
        ]

    def _release_locks(self, locks: list[asyncio.Lock]) -> None:
        """Release in reverse order, then release keyed-table references."""
        for lock in reversed(locks):
            lock.release()
        for lock in locks:
            self._d._resource_locks.release_reference(lock)

    def _executor_for(self, request: CapabilityRequest, workspace: WorkspaceSpec):
        fabric = self._d._fabric
        if fabric is not None:
            return fabric.executor_for(
                request.capability_id,
                task_id=request.task_id,
                project_id=workspace.id,
                user_id=self._d._principal.id,
            )
        return self._d.registry.executor_for(request.capability_id)


class DispatchRepair(_Mechanism):
    """Batch preflight validation/repair and durable repair receipts."""

    async def prepare_one(
        self,
        request: CapabilityRequest,
        workspace: WorkspaceSpec,
        *,
        provenance: DispatchProvenance | None = None,
        directives: DispatchDirectives | None = None,
    ) -> PreparedCapabilityCall:
        """Repair, validate, and resolve one call into one invocation snapshot."""
        return (
            await self._prepare_each(
                [request],
                workspace=workspace,
                provenance=provenance,
                directives_by_call_id={request.call_id: directives}
                if directives is not None
                else None,
            )
        )[0]

    async def _preflight_batch(
        self,
        requests: list[CapabilityRequest],
        *,
        workspace: WorkspaceSpec,
        provenance: DispatchProvenance | None = None,
        directives_by_call_id: Mapping[str, DispatchDirectives] | None = None,
    ) -> list[PreparedCapabilityCall]:
        """Canonicalize every call before any batch member executes."""
        return [
            await self.prepare_one(
                request,
                workspace,
                provenance=provenance,
                directives=directives_by_call_id.get(request.call_id)
                if directives_by_call_id is not None
                else None,
            )
            for request in requests
        ]

    @staticmethod
    def preflight_issues(prepared_calls: list[PreparedCapabilityCall]) -> list[str]:
        """Rebuild stable, order-aligned issue paths from failed entries."""
        issues: list[str] = []
        for index, prepared in enumerate(prepared_calls):
            if prepared.executor is not None:
                continue
            path = f"[{index}] {prepared.capability_id}"
            failure = prepared.failure
            if failure is not None:
                detail = failure.detail
                if failure.schema_errors:
                    detail = "; ".join(failure.schema_errors)
                issues.append(f"{path}: {failure.code} {detail}")
                continue
            if isinstance(prepared.request.arguments, str):
                issues.append(f"{path}: tool_input_invalid invalid JSON arguments")
            else:
                issues.append(f"{path}: schema_validation invalid arguments")
        return issues

    async def _prepare_each(
        self,
        requests: list[CapabilityRequest],
        *,
        workspace: WorkspaceSpec,
        provenance: DispatchProvenance | None = None,
        directives_by_call_id: Mapping[str, DispatchDirectives] | None = None,
    ) -> list[PreparedCapabilityCall]:
        """Canonicalize every call exactly once.

        A failed entry carries no executor, so a batch can reject every call
        without touching an executor a second time.
        """
        from athena.models.compat.candidates import get_raw_candidate

        prepared_calls: list[PreparedCapabilityCall] = []
        receipt = None
        for request in requests:
            if not request.call_id:
                request = replace(request, call_id=new_id("call"))
            try:
                executor = self._d._executor_for(request, workspace)
            except (CapabilityUnavailable, KeyError, TypeError, ValueError) as exc:
                prepared_calls.append(
                    PreparedCapabilityCall(
                        request=request,
                        workspace=workspace,
                        executor=None,
                        effects=(),
                        failure=PreparationFailure(code="unknown_capability", detail=str(exc)),
                    )
                )
                continue

            # Mirror dispatch()'s repair inputs (raw-candidate aware).
            repair_arguments = dict(request.arguments or {})
            candidate = request.candidate or get_raw_candidate(request.call_id)
            completion_state = "CLEAN"
            if (
                candidate is not None
                and candidate.parsed_arguments is None
                and isinstance(candidate.raw_arguments, str)
                # Empty raw input is significant: it is an incomplete or
                # malformed model candidate, not a valid empty object.
                and candidate.raw_arguments.strip() != "{}"
            ):
                repair_arguments = candidate.raw_arguments  # type: ignore[assignment]
                completion_state = candidate.completion_state

            if getattr(request.origin, "value", request.origin) == "model":
                repaired_args, receipt = self._d.repairer.repair(
                    call_id=request.call_id,
                    tool_name=request.capability_id,
                    arguments=repair_arguments,
                    input_schema=executor.descriptor.input_schema,
                    validate_fn=validate_schema,
                    mcp_origin=(getattr(executor.descriptor.origin, "value", None) == "MCP"),
                    provider_profile_id=(
                        getattr(candidate, "provider_profile_id", None)
                        or (provenance.provider_profile_id if provenance else None)
                    ),
                    model_id=getattr(candidate, "model_id", None)
                    or (provenance.model_id if provenance else None),
                    completion_state=completion_state,
                    mode=(provenance.repair_mode if provenance else None),
                )
                if receipt.outcome == "INVALID":
                    await self._d._persist_repair(
                        request,
                        receipt,
                        original_arguments=repair_arguments,
                        canonical_arguments=None,
                    )
                    prepared_calls.append(
                        PreparedCapabilityCall(
                            request=request,
                            workspace=workspace,
                            executor=None,
                            effects=(),
                            failure=PreparationFailure(
                                code="repair_invalid",
                                detail=f"repair outcome {receipt.outcome}",
                                repair_receipt=receipt,
                            ),
                        )
                    )
                    continue
                if receipt.outcome == "REPAIRED":
                    await self._d._persist_repair(
                        request,
                        receipt,
                        original_arguments=repair_arguments,
                        canonical_arguments=repaired_args,
                    )
                    request = replace(request, arguments=dict(repaired_args))
                    await self._d._emit_repair(request, receipt, repaired_args)
                else:
                    await self._d._persist_repair(
                        request,
                        receipt,
                        original_arguments=repair_arguments,
                        canonical_arguments=request.arguments,
                    )

            errors = validate_schema(executor.descriptor.input_schema, request.arguments or {})
            if errors:
                prepared_calls.append(
                    PreparedCapabilityCall(
                        request=request,
                        workspace=workspace,
                        executor=None,
                        effects=(),
                        failure=PreparationFailure(
                            code="schema_validation",
                            detail="schema validation failed",
                            schema_errors=tuple(str(e) for e in errors),
                        ),
                    )
                )
                continue

            try:
                effects = await self._d._resolve_executor_effects(executor, request, workspace)
            except ValueError as exc:
                prepared_calls.append(
                    PreparedCapabilityCall(
                        request=request,
                        workspace=workspace,
                        executor=None,
                        effects=(),
                        failure=PreparationFailure(
                            code="effect_contract",
                            detail=str(exc),
                        ),
                    )
                )
                continue

            prepared_calls.append(
                PreparedCapabilityCall(
                    request=request,
                    workspace=workspace,
                    executor=executor,
                    effects=effects,
                    directives=directives_by_call_id.get(request.call_id)
                    if directives_by_call_id is not None
                    else None,
                    resource_keys=DispatchOrdering(self._d).resource_keys_for(
                        request, workspace, effects, executor=executor
                    ),
                    canonical_request=request,
                    descriptor=getattr(executor, "descriptor", None),
                    repair_receipt=receipt,
                    registry_generation=getattr(self._d.registry, "generation", None),
                )
            )

        return prepared_calls

    async def _persist_repair(
        self,
        request: CapabilityRequest,
        receipt,
        *,
        original_arguments: Any,
        canonical_arguments: Mapping[str, Any] | None,
    ) -> None:
        """Make a repair receipt durable before policy or execution."""
        if self._d._repair_store is None:
            return
        try:
            await self._d._repair_store.record(
                task_id=request.task_id,
                capability_id=request.capability_id,
                origin=str(getattr(request.origin, "value", request.origin)),
                receipt=receipt.to_dict(),
                original_arguments=original_arguments,
                canonical_arguments=canonical_arguments,
            )
        except Exception as exc:
            raise PersistenceError(
                f"tool repair receipt persistence failed for {request.call_id}: {exc}",
                cause=exc,
            ) from exc

    async def _emit_repair(self, request: CapabilityRequest, receipt, canonical_arguments) -> None:
        await self._d._emit(
            "ToolRepaired",
            {
                "call_id": request.call_id,
                "tool_name": request.capability_id,
                "rules": receipt.rules,
                "policy_version": receipt.repair_policy_version,
                "schema_hash": receipt.schema_hash,
                "provider_profile_id": receipt.provider_profile_id,
                "model_id": receipt.model_id,
                "original_shape_hash": receipt.original_shape_hash,
                "repaired_shape_hash": receipt.repaired_shape_hash,
                "canonical_arguments": dict(canonical_arguments or {}),
            },
            request.task_id,
            causal_id=request.call_id,
        )

    # ------------------------------------------------------------------ #
    # Resolution / mutation
    # ------------------------------------------------------------------ #
    # Capabilities whose operations are process/code operations, not file
    # writes — even when an op name like "create" would suggest a write.


_VERIFICATION_EFFECT_FLOOR = frozenset(
    {
        EffectClass.READ_LOCAL,
        EffectClass.WRITE_LOCAL,
        EffectClass.EXECUTE,
        EffectClass.SPAWN_PROCESS,
        EffectClass.NETWORK_READ,
    }
)


def _combine_verdicts(
    task_verdict: PolicyVerdict | None,
    global_verdict: PolicyVerdict,
    global_reason: str,
) -> tuple[PolicyVerdict, str]:
    """Merge task-policy and global verdicts, keeping the STRICTEST (P0-7).

    The task policy is a hard ceiling: global policy may narrow further but never
    expand task authority. Returns the combined verdict and a human reason.
    """
    if task_verdict is None or task_verdict == PolicyVerdict.ALLOW:
        return global_verdict, global_reason
    if _STRICTNESS[task_verdict.value] >= _STRICTNESS[global_verdict.value]:
        return task_verdict, "required by task capability policy"
    return global_verdict, global_reason


def _verdict(value) -> PolicyVerdict:
    if isinstance(value, PolicyVerdict):
        return value
    v = str(value or "").lower()
    if v == "allow":
        return PolicyVerdict.ALLOW
    if v == "deny":
        return PolicyVerdict.DENY
    return PolicyVerdict.ASK


# High-risk effects that should default to CALL scope even when the operator
# chooses TASK or SESSION.  These effects have blast radius beyond a single
# localized operation: network egress, external messages, privilege, secrets,
# financial impact, and computer input.  Reusable grants for these are too
# coarse — the operator should bind an explicit authority envelope.
def _wrap_exception(exc, request) -> CapabilityResult:
    call_id = getattr(request, "call_id", None) or ""
    return CapabilityResult(
        call_id,
        getattr(request, "capability_id", ""),
        CapabilityResultStatus.FAILED,
        error=f"dispatch failed: {exc}",
    )


class PolicyGate(_Mechanism):
    """Effect resolution, task-policy ceiling, approval parking."""

    @staticmethod
    def _exec_capabilities():
        from athena.capabilities.dispatch_helpers import EXEC_CAPABILITIES

        return EXEC_CAPABILITIES

    @staticmethod
    def _resolve_effects_for(descriptor, arguments: Mapping[str, Any]) -> tuple[EffectClass, ...]:
        """Contract-first effect resolution (P0-9).

        Capabilities with declared operation maps use their own exact
        classification; unknown operations FAIL rather than guess. Legacy
        heuristic applies only to capabilities without a map.
        """
        from athena.capabilities.operations import (
            CapabilityEffectError,
            resolve_operation_effects,
        )

        try:
            contract_effects = resolve_operation_effects(descriptor, arguments)
        except CapabilityEffectError as exc:
            raise ValueError(str(exc)) from None
        if contract_effects:
            return contract_effects
        return PolicyGate._resolve_effects(descriptor, arguments)

    @staticmethod
    def _resolve_effects(descriptor, arguments: Mapping[str, Any]) -> tuple[EffectClass, ...]:
        """Resolve the full concrete effect set for bound arguments (BHV-041).

        Multi-effect operations (copy/move, execute) return the combined set so
        the PolicyEngine evaluates every effect, not just one.
        """
        available = descriptor.effects
        op = str(arguments.get("operation") or arguments.get("action") or "").lower()
        want: tuple[EffectClass, ...]

        if descriptor.id in PolicyGate._exec_capabilities():
            # Code/process capabilities: every operation is execution-shaped.
            # READ_LOCAL covers screen/output inspection; WRITE_LOCAL covers
            # side effects the spawned process may have.
            want = (EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS)
        elif op in ("copy", "move"):
            want = (EffectClass.READ_LOCAL, EffectClass.WRITE_LOCAL)
        elif op in ("write", "patch", "mkdir", "create", "update", "save"):
            want = (EffectClass.WRITE_LOCAL,)
        elif op in ("delete", "remove", "rmtree", "unlink"):
            want = (EffectClass.DELETE,)
        elif op in ("read", "list", "stat", "get", "exists", "open", "recall"):
            want = (EffectClass.READ_LOCAL,)
        elif op in ("", "execute", "run", "exec"):
            want = (EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS)
        else:
            want = ()
        effects = tuple(e for e in want if e in available)
        if effects:
            return effects
        if EffectClass.EXECUTE in available or EffectClass.SPAWN_PROCESS in available:
            return tuple(
                e for e in (EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS) if e in available
            )
        fallback = _primary_effect(available)
        if fallback is not None:
            return (fallback,)
        return tuple(sorted(available, key=lambda e: e.value))

    @staticmethod
    def _eval_task_policy(
        capability_id: str,
        task_policy: CapabilityPolicy | None,
        request_effects: frozenset[EffectClass] | None = None,
    ) -> PolicyVerdict | None:
        """Evaluate the task's capability policy as a HARD ceiling (P0-7).

        Returns None when the task fully allows the capability (no narrowing);
        otherwise a PolicyVerdict.DENY (hard, no override) or ASK. ``deny`` is a
        hard no (BHV-043); global policy can only narrow, never expand.

        Enforces both capability-ID allowlists AND effect ceilings: if the task
        policy declares effects, the request's resolved effects must be a subset.
        """
        if task_policy is None:
            return None
        if not capability_id_permitted(capability_id, task_policy):
            return PolicyVerdict.DENY
        if capability_id in task_policy.ask:
            return PolicyVerdict.ASK
        # Enforce effect ceiling: task effects must cover request effects
        if (
            request_effects
            and task_policy.effects
            and not request_effects.issubset(task_policy.effects)
        ):
            return PolicyVerdict.DENY
        return None

    async def _deny_path(
        self,
        *,
        request: CapabilityRequest,
        policy_failure: CapabilityResult | None,
        reason: str,
    ) -> CapabilityResult:
        """Resolve the canonical DENY outcome (typed or prose failure)."""
        error = (
            "verification call requires effects outside the bounded verification envelope"
            if policy_failure is not None
            else f"denied: {reason or 'policy'}"
        )
        await self._d._emit(
            EV["CAPABILITY_FAILED"],
            {"call_id": request.call_id, "reason": "denied"},
            request.task_id,
            causal_id=request.call_id,
        )
        return (
            policy_failure
            if policy_failure is not None
            else CapabilityResult(
                request.call_id,
                request.capability_id,
                CapabilityResultStatus.FAILED,
                error=error,
            )
        )

    async def _ask_path(
        self,
        *,
        request: CapabilityRequest,
        workspace: WorkspaceSpec,
        executor,
        effects,
        profile: str | None,
        receipt,
        directives: DispatchDirectives | None,
        provenance: DispatchProvenance | None,
    ):
        """Park the call for approval and register its durable continuation."""
        from athena.protocol.continuations import SuspendedCall

        decision = self._d.policy.evaluate(
            PolicyRequest(
                principal=self._d._principal,
                task_id=request.task_id,
                capability_id=request.capability_id,
                arguments=dict(request.arguments or {}),
                workspace=workspace,
                execution_backend=workspace.execution_backend or "local",
                effects=frozenset(effects),
                resources=executor.descriptor.resolve_resources(),
                session_id=getattr(request, "session_id", None),
                call_id=request.call_id,
            ),
            autonomy=profile,
        )
        approval_id = await self._park_for_approval(
            request,
            decision=decision,
            workspace=workspace,
            arguments=dict(request.arguments or {}),
            effects=effects,
            receipt=receipt,
            directives=directives,
            provenance=provenance,
        )
        suspended = SuspendedCall(
            request.call_id,
            request,
            self._d.policy.evaluate(
                PolicyRequest(
                    principal=self._d._principal,
                    task_id=request.task_id,
                    capability_id=request.capability_id,
                    arguments=dict(request.arguments or {}),
                    workspace=workspace,
                    execution_backend=workspace.execution_backend or "local",
                    effects=frozenset(effects),
                    resources=executor.descriptor.resolve_resources(),
                    session_id=getattr(request, "session_id", None),
                    call_id=request.call_id,
                ),
                autonomy=profile,
            ),
            approval_id,
            directives,
        )
        self._d._suspended[request.call_id] = suspended
        await self._d._emit(
            EV["APPROVAL_REQUESTED"],
            {
                "call_id": request.call_id,
                "capability_id": request.capability_id,
                "task_id": request.task_id,
                "approval_id": approval_id,
            },
            request.task_id,
            causal_id=request.call_id,
        )
        return suspended

    async def _route_reality(
        self,
        *,
        request: CapabilityRequest,
        workspace: WorkspaceSpec,
        executor,
        effects,
        directives: DispatchDirectives | None,
    ) -> tuple[WorkspaceSpec, dict[Any, Any], Any | None, bool]:
        """Route through the reality boundary; return workspace/metadata/route/isolated."""
        from athena.protocol.capabilities import CapabilityResultStatus
        from athena.protocol.reality import ExecutionDisposition

        if self._d._reality_gate is None:
            return workspace, {}, None, False
        try:
            route = await self._d._reality_gate.route(
                request,
                workspace,
                frozenset(effects),
                executor.descriptor,
                tier=(directives.reality_tier if directives is not None else None),
            )
        except PermissionError as exc:
            result = CapabilityResult(
                request.call_id,
                request.capability_id,
                CapabilityResultStatus.FAILED,
                error=str(exc),
                metadata={"decision": "reality_boundary", "code": "reality_boundary_denied"},
            )
            await self._d._emit(
                EV["CAPABILITY_FAILED"],
                {
                    "call_id": request.call_id,
                    "capability_id": request.capability_id,
                    "reason": "reality_boundary",
                    "error": result.error,
                },
                request.task_id,
                causal_id=request.call_id,
            )
            raise _RealityBoundaryDenied(result) from exc
        isolated = route.disposition is ExecutionDisposition.ISOLATED
        return route.workspace, route.metadata(), route, isolated

    async def _park_for_approval(
        self,
        request: CapabilityRequest,
        *,
        decision: PolicyDecision,
        workspace: WorkspaceSpec,
        arguments: Mapping[str, Any],
        effects,
        receipt=None,
        directives: DispatchDirectives | None = None,
        provenance: DispatchProvenance | None = None,
    ) -> str | None:
        """Persist an ApprovalRequest and register it for later resolution.

        Durable persistence (ApprovalStore) and the in-memory ApprovalManager
        record use the SAME approval_id so ``AthenaService.approve`` can resolve
        either. The grant binds to the exact argument digest (TOCTOU guard).
        """
        scope = ApprovalScope.CALL
        if decision.approval_scope_options:
            scope = decision.approval_scope_options[0]
        # High-risk effects (network write, external message, secret read,
        # financial, privileged) should default to CALL scope even when the
        # operator picks TASK/SESSION.  A reusable grant for these is too
        # coarse — the blast radius crosses task boundaries.
        effect_set = set(effects)
        if scope != ApprovalScope.CALL and effect_set & _high_risk_effects():
            scope = ApprovalScope.CALL
        digest = args_digest(arguments)
        # ApprovalManager currently stores naive UTC datetimes. Keep that
        # legacy contract while deriving the value from an explicit UTC clock.
        from athena.protocol.messages import utcnow

        expires_at = utcnow().replace(tzinfo=None) + timedelta(hours=24)
        approval_id = new_id("apr")
        metadata = {
            "call_id": request.call_id,
            "args_digest": digest,
            "capability_id": request.capability_id,
            "effects": [e.value for e in effects],
            "workspace": workspace.id,
            "execution_backend": workspace.execution_backend or "local",
            "scope": scope.value,
            "requested_scope": [s.value for s in decision.approval_scope_options],
            "expires_at": expires_at.isoformat(),
            # SESSION-scoped grants must survive resolution; keyed on this.
            "session_id": getattr(request, "session_id", None),
        }

        if self._d._approval_store is not None:
            persisted = await self._d._approval_store.create_request(
                task_id=request.task_id,
                capability_id=request.capability_id,
                arguments=dict(arguments),
                approval_id=approval_id,
                metadata=metadata,
            )
            approval_id = persisted

        manager = getattr(self._d.policy, "approvals", None)
        if manager is not None and manager.state(approval_id) is None:
            primary = _primary_effect(tuple(effects))
            manager.create_request(
                self._d._principal,
                scope,
                capability=request.capability_id,
                effect=str(primary.value) if primary is not None else None,
                # Authority envelope (P0): the grant binds to the COMPLETE
                # resolved effect set the operator is approving, not just
                # the primary effect. A resumed or generalized call whose
                # effects exceed this ceiling is not covered.
                allowed_effects=tuple(effects),
                task_id=request.task_id,
                session_id=getattr(request, "session_id", None),
                expires_at=expires_at,
                approval_id=approval_id,
                args_digest=digest,
                call_id=request.call_id,
            )

        if self._d._continuation_store is not None:
            # Durable continuation (review item 19): the kernel's parked call
            # is in-memory only, so persist enough to reconstruct it after a
            # restart. Failure-isolated — approval parking must not break.
            try:
                await self._d._continuation_store.record(
                    task_id=request.task_id,
                    call_id=request.call_id,
                    capability_id=request.capability_id,
                    canonical_arguments=dict(arguments),
                    schema_hash=getattr(receipt, "schema_hash", None),
                    effects=tuple(effects or ()),
                    workspace_id=workspace.id,
                    approval_id=approval_id,
                    provider_profile_id=(
                        getattr(request.candidate, "provider_profile_id", None)
                        or (provenance.provider_profile_id if provenance else None)
                    ),
                    model_id=(
                        getattr(request.candidate, "model_id", None)
                        or (provenance.model_id if provenance else None)
                    ),
                    repair_policy_version=getattr(receipt, "repair_policy_version", None),
                    policy_context={
                        "origin": getattr(request.origin, "value", request.origin),
                        "session_id": getattr(request, "session_id", None),
                        **(
                            {
                                "workflow_run_id": directives.workflow_run_id,
                                "workflow_step_id": directives.workflow_step_id,
                                "workflow_item_index": directives.workflow_item_index,
                                "workflow_execution_id": directives.workflow_execution_id,
                                "workflow_parent_call_id": directives.workflow_parent_call_id,
                                "workflow_parent_capability_id": directives.workflow_parent_capability_id,
                                "workflow_id": directives.workflow_id,
                            }
                            if directives is not None and directives.workflow_run_id
                            else {}
                        ),
                    },
                )
            except Exception as exc:
                # An approval without a durable canonical continuation is not
                # safe to expose as resumable work. Fail closed instead of
                # silently reverting to process-local SuspendedCall state.
                raise PersistenceError(
                    f"approval continuation persistence failed: {exc}",
                    cause=exc,
                ) from exc

        self._d._resume_expiry[approval_id] = expires_at
        return approval_id

    async def _evaluate_call_policy(
        self,
        *,
        request: CapabilityRequest,
        workspace: WorkspaceSpec,
        executor,
        effects: tuple[EffectClass, ...],
        profile: str | None,
        task_policy: CapabilityPolicy | None,
        directives: DispatchDirectives | None,
    ) -> tuple[PolicyVerdict, str, CapabilityResult | None, frozenset[EffectClass]]:
        """Evaluate global, task, verifier, external, and orchestration policy."""
        policy_request = PolicyRequest(
            principal=self._d._principal,
            task_id=request.task_id,
            capability_id=request.capability_id,
            arguments=dict(request.arguments or {}),
            workspace=workspace,
            execution_backend=workspace.execution_backend or "local",
            effects=frozenset(effects),
            resources=executor.descriptor.resolve_resources(),
            session_id=getattr(request, "session_id", None),
            call_id=request.call_id,
        )
        decision = self._d.policy.evaluate(policy_request, autonomy=profile)
        global_verdict = _verdict(decision.decision)

        # The task capability policy is a ceiling on the task's work surface.
        # SYSTEM-origin calls are the host auditing the task's own declared
        # state (acceptance-criteria verification, internal memory recall):
        # never model-controlled, never dispatched from natural language. A
        # task that denies every capability must still be verifiable against
        # the criteria it declared, so the host's own observation floor is not
        # narrowed by the task's ceiling.
        origin_value = getattr(request.origin, "value", request.origin)
        host_observation = origin_value == CapabilityRequestOrigin.SYSTEM.value
        # SYSTEM_VERIFICATION is the bounded verifier authority (P0-7): the
        # acceptance verifier may execute operator-declared criteria, but it
        # inherits none of the task's capability ceiling. It is still held to
        # a restricted effect envelope — observation plus bounded execution,
        # never secrets, privilege, external publication, or computer input.
        verifier_authority = origin_value == CapabilityRequestOrigin.SYSTEM_VERIFICATION.value
        if verifier_authority and not _VERIFICATION_EFFECT_FLOOR.issuperset(effects):
            await self._d._emit(
                EV["CAPABILITY_FAILED"],
                {
                    "call_id": request.call_id,
                    "capability_id": request.capability_id,
                    "reason": "verification_effect_ceiling",
                    "effects": sorted(effect.value for effect in effects),
                },
                request.task_id,
                causal_id=request.call_id,
            )
            return (
                PolicyVerdict.DENY,
                "verification call requires effects outside the bounded verification envelope",
                CapabilityResult(
                    request.call_id,
                    request.capability_id,
                    CapabilityResultStatus.FAILED,
                    error=(
                        "verification call requires effects outside the bounded verification envelope"
                    ),
                ),
                frozenset(),
            )
        task_verdict = (
            None
            if host_observation or verifier_authority
            else self._eval_task_policy(
                request.capability_id, task_policy, request_effects=frozenset(effects)
            )
        )
        combined, reason = _combine_verdicts(task_verdict, global_verdict, decision.reason)
        external_contract = executor.descriptor.resolve_external_effect_contract(
            request.arguments or {}
        )
        external_phase = str((request.arguments or {}).get("phase") or "").lower()
        explicit_approval = str(decision.matched_rule or "").startswith("approval:")
        external_floor_requires_approval = (
            external_contract is not None
            and external_contract.approval_floor == "ask"
            and external_phase
            in {
                ExternalEffectPhase.APPLY.value,
                ExternalEffectPhase.COMPENSATE.value,
            }
            and not explicit_approval
        )
        if external_contract is not None and external_contract.approval_floor == "deny":
            combined = PolicyVerdict.DENY
            reason = "forbidden by external-effect contract"
        elif external_floor_requires_approval and combined is PolicyVerdict.ALLOW:
            # A contract floor narrows an ordinary allow. An explicit
            # operator grant is the one permitted exception; PolicyEngine
            # marks those decisions with an approval rule.
            combined = PolicyVerdict.ASK
            reason = "requires approval by external-effect contract"
        orchestration_approval_id = directives.approval_id if directives is not None else None
        trusted_approval = (
            orchestration_approval_id
            and getattr(request.origin, "value", request.origin) == "trusted_orchestration"
        )
        if (
            trusted_approval
            and global_verdict is PolicyVerdict.ASK
            and task_verdict in {None, PolicyVerdict.ALLOW}
            and not external_floor_requires_approval
            and not (external_contract is not None and external_contract.approval_floor == "deny")
        ):
            combined = PolicyVerdict.ALLOW
            reason = f"approved orchestration plan {orchestration_approval_id}"
        inherited_effects = frozenset(getattr(directives, "inherited_effects", ()))
        generated_origin = (
            getattr(request.origin, "value", request.origin)
            == CapabilityRequestOrigin.GENERATED.value
        )
        if generated_origin and inherited_effects and not set(effects).issubset(inherited_effects):
            combined = PolicyVerdict.DENY
            reason = (
                "generated call attempts effects outside its parent authority ceiling: "
                f"{sorted(effect.value for effect in set(effects) - inherited_effects)}"
            )
        if (
            combined == PolicyVerdict.ASK
            and inherited_effects
            and set(effects).issubset(inherited_effects)
            and task_verdict in {None, PolicyVerdict.ALLOW}
            and not external_floor_requires_approval
            and not (external_contract is not None and external_contract.approval_floor == "deny")
        ):
            combined = PolicyVerdict.ALLOW
            reason = (
                "authorized by generated parent capability "
                f"{getattr(directives, 'inherited_capability_id', None) or 'call'}"
            )
        await self._d._emit(
            EV["POLICY_DECISION_MADE"],
            {
                "call_id": request.call_id,
                "capability_id": request.capability_id,
                "decision": combined.value,
                "reason": reason,
                "matched_rule": decision.matched_rule,
            },
            request.task_id,
            causal_id=request.call_id,
        )
        return combined, reason, None, inherited_effects
