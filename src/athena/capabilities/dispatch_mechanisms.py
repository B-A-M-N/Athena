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
import os
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Mapping

from athena.capabilities.registry import validate_schema
from athena.policy.approvals import args_digest
from athena.protocol.capabilities import (
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    DispatchDirectives,
    EffectClass,
)
from athena.protocol.errors import CapabilityUnavailable, PersistenceError
from athena.protocol.ids import new_id
from athena.protocol.events import EV
from athena.protocol.policy import ApprovalScope, PolicyDecision, PolicyVerdict
from athena.protocol.tasks import CapabilityPolicy, capability_id_permitted
from athena.protocol.tasks import ResourceBudget, WorkspaceSpec

if TYPE_CHECKING:
    from athena.capabilities.dispatcher import CapabilityDispatcher

__all__ = ["DispatchOrdering", "DispatchRepair", "PolicyGate", "ResultCache"]

_logger = logging.getLogger("athena.capabilities")


def _mod():
    from athena.capabilities import dispatcher as m

    return m


def _primary_effect(available):
    return _mod()._primary_effect(available)


def _resource_key(workspace, value):
    return _mod()._resource_key(workspace, value)


def _is_ordering_sensitive(effects):
    return _mod()._is_ordering_sensitive(effects)


def _is_execution(effects):
    return _mod()._is_execution(effects)


def _bounded_diagnostics(values):
    return _mod()._bounded_diagnostics(values)


def _high_risk_effects():
    # Bound lazily: the constant lives at the bottom of the dispatcher module.
    return _mod().HIGH_RISK_EFFECTS


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
    ) -> list[asyncio.Lock] | list[asyncio.Lock]:
        """Ordering for one call, or [] when it may parallelize.

        Resource-ambiguous dependency-bearing calls — a write/delete/child/
        external with no concrete named resource — cannot prove independence
        from siblings mutating ambient state, so they serialize against each
        other in model order (asyncio.Lock is FIFO). Named-path calls
        (already per-resource-locked, different paths independently parallel)
        and pure reads bypass it.
        """
        if batch_order_lock is None:
            return []
        if not _is_ordering_sensitive(effects):
            return []
        if self._d._locks_for_request(request, workspace, effects):
            return []
        return [batch_order_lock]

    async def _dispatch_with_controls(
        self,
        request: CapabilityRequest,
        *,
        workspace: WorkspaceSpec,
        profile: str | None,
        task_policy: CapabilityPolicy | None,
        model_policy: Any,
        task_budget: ResourceBudget | None,
        task_deadline: datetime | None,
        runtime_remaining_s: float | None,
        verification_environment: Any,
        directives: DispatchDirectives | None,
        prepared: bool,
        batch_order_lock: asyncio.Lock | None = None,
    ):
        """Apply task concurrency and resource conflict controls.

        The capability itself remains the authority for effects.  This helper
        only uses the already-declared contract to prevent an unbounded batch
        from exceeding the task budget or racing requests targeting the same
        resource.
        """
        effects: tuple[EffectClass, ...] = ()
        try:
            executor = self._d._executor_for(request, workspace)
            effects = self._d._resolve_effects_for(executor.descriptor, request.arguments or {})
        except (CapabilityUnavailable, KeyError, TypeError, ValueError):
            # dispatch() will return the canonical validation/effect error.
            pass

        locks = self._d._locks_for_request(request, workspace, effects)

        # Resource-ambiguous dependency-bearing calls join a per-batch
        # ordering lane; named-path calls (already in ``locks``) and pure
        # reads do not. Ambiguous means: carries a dependency-bearing effect
        # but resolved no concrete resource key, so it cannot prove
        # independence from sibling mutations/executes on ambient state.
        order = self._d._batch_order(request, workspace, effects, batch_order_lock)

        async def invoke_with_locks():
            for lock in order:
                await lock.acquire()
            for lock in locks:
                await lock.acquire()
            try:
                return await self._d.dispatch(
                    request,
                    workspace=workspace,
                    profile=profile,
                    task_policy=task_policy,
                    model_policy=model_policy,
                    task_budget=task_budget,
                    task_deadline=task_deadline,
                    runtime_remaining_s=runtime_remaining_s,
                    verification_environment=verification_environment,
                    _directives=directives,
                    _prepared=prepared,
                )
            finally:
                for lock in reversed(locks):
                    lock.release()
                for lock in reversed(order):
                    lock.release()

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

    def _locks_for_request(
        self,
        request: CapabilityRequest,
        workspace: WorkspaceSpec,
        effects: tuple[EffectClass, ...],
    ) -> list[asyncio.Lock]:
        if not set(effects) & {
            EffectClass.READ_LOCAL,
            EffectClass.WRITE_LOCAL,
            EffectClass.DELETE,
        }:
            return []
        args = request.arguments or {}
        raw_resources = [args.get("path"), args.get("destination")]
        resources = {
            _resource_key(workspace, value)
            for value in raw_resources
            if isinstance(value, str) and value
        }
        if not resources:
            return []
        locks: list[asyncio.Lock] = []
        for resource in sorted(resources):
            key = (workspace.id or workspace.root, resource)
            locks.append(self._d._resource_locks.setdefault(key, asyncio.Lock()))
        return locks

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

    async def _preflight_batch(
        self,
        requests: list[CapabilityRequest],
        *,
        workspace: WorkspaceSpec,
    ) -> list[str]:
        """Validate/repair EVERY call before ANY executes; mutate nothing but
        canonicalize repaired arguments via ``object.__setattr__`` (same as
        ``dispatch``). Returns a list of issue paths (empty = all clear)."""
        from athena.models.compat.candidates import get_raw_candidate

        issues: list[str] = []
        for index, request in enumerate(requests):
            path = f"[{index}] {request.capability_id}"
            if not request.call_id:
                object.__setattr__(request, "call_id", new_id("call"))
            try:
                executor = self._d._executor_for(request, workspace)
            except (CapabilityUnavailable, KeyError, TypeError, ValueError) as exc:
                issues.append(f"{path}: unknown-capability ({exc})")
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
                        or self._d._provider_profile_id
                    ),
                    model_id=getattr(candidate, "model_id", None) or self._d._model_id,
                    completion_state=completion_state,
                    mode=self._d._repair_mode,
                )
                if receipt.outcome == "INVALID":
                    await self._d._persist_repair(
                        request,
                        receipt,
                        original_arguments=repair_arguments,
                        canonical_arguments=None,
                    )
                    issues.append(
                        f"{path}: tool_input_invalid "
                        + ("; ".join(receipt.issue_codes) or "invalid arguments")
                    )
                    continue
                if receipt.outcome == "REPAIRED":
                    await self._d._persist_repair(
                        request,
                        receipt,
                        original_arguments=repair_arguments,
                        canonical_arguments=repaired_args,
                    )
                    object.__setattr__(request, "arguments", repaired_args)
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
                issues.append(f"{path}: schema_validation ({'; '.join(errors)})")
                continue

            try:
                self._d._resolve_effects_for(executor.descriptor, request.arguments or {})
            except ValueError as exc:
                issues.append(f"{path}: effects_unresolved ({exc})")

        return issues

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


class PolicyGate(_Mechanism):
    """Effect resolution, task-policy ceiling, approval parking."""

    @staticmethod
    def _exec_capabilities():
        # Single-sourced on the owning dispatcher class; bound lazily to
        # avoid the module cycle.
        return _mod().CapabilityDispatcher._EXEC_CAPABILITIES

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
        return _mod().CapabilityDispatcher._resolve_effects(descriptor, arguments)

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
                        or self._d._provider_profile_id
                    ),
                    model_id=(getattr(request.candidate, "model_id", None) or self._d._model_id),
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


class ResultCache(_Mechanism):
    """Health persistence, observations, failure memory, result cache."""

    async def _persist_health(self, capability_id: str) -> None:
        persist = getattr(self._d._health, "persist", None)
        if persist is None:
            return
        try:
            await persist(capability_id)
            mark_persisted = getattr(self._d._health, "mark_persisted", None)
            if callable(mark_persisted):
                mark_persisted(capability_id)
        except Exception as exc:  # health persistence must not alter call truth
            await self._d._emit(
                "CapabilityHealthChanged",
                {
                    "capability_id": capability_id,
                    "persistence_error": str(exc)[:500],
                },
                None,
                causal_id=capability_id,
            )

    def _health_should_persist(self, capability_id: str, state_changed: bool) -> bool:
        should_persist = getattr(self._d._health, "should_persist", None)
        if callable(should_persist):
            return bool(should_persist(capability_id, state_changed=state_changed))
        return True

    async def _emit_result_observations(
        self,
        request: CapabilityRequest,
        result: CapabilityResult,
    ) -> None:
        """Publish structured result evidence for projections and replay."""
        metadata = result.metadata or {}
        diagnostics = metadata.get("diagnostics")
        if not isinstance(diagnostics, (list, tuple)) or not diagnostics:
            return
        await self._d._emit(
            EV["DIAGNOSTICS_PRODUCED"],
            {
                "call_id": request.call_id,
                "capability_id": request.capability_id,
                "diagnostics": _bounded_diagnostics(diagnostics),
                "count": len(diagnostics),
            },
            request.task_id,
            causal_id=request.call_id,
        )

    async def _attach_failure_memory(
        self,
        request: CapabilityRequest,
        result: CapabilityResult,
        workspace,
    ) -> None:
        """Attach advisory deterministic repair history to failed results."""
        if self._d._failure_memory is None or result.status is CapabilityResultStatus.OK:
            return
        diagnostics = (result.metadata or {}).get("diagnostics")
        if not isinstance(diagnostics, (list, tuple)):
            return
        suggestions: list[dict[str, Any]] = []
        environment = str((result.metadata or {}).get("failure_environment_fingerprint") or "")
        for item in diagnostics[:8]:
            if not isinstance(item, Mapping):
                continue
            signature = str(item.get("signature_fingerprint") or item.get("fingerprint") or "")
            if not signature:
                continue
            try:
                suggestions.extend(
                    await self._d._failure_memory.retrieve(
                        signature_fingerprint=signature,
                        capability_id=request.capability_id,
                        environment_fingerprint=environment,
                        project_scope=getattr(workspace, "id", None),
                        limit=4,
                    )
                )
            except Exception:
                continue
        if suggestions:
            metadata = dict(result.metadata or {})
            metadata["failure_memory"] = suggestions[:8]
            object.__setattr__(result, "metadata", metadata)

    def _cached_result(
        self,
        key: tuple[str | None, str, str, str, str | None],
    ) -> CapabilityResult | None:
        entry = self._d._result_cache.get(key)
        if entry is None:
            return None
        expires_at, result = entry
        if time.monotonic() >= expires_at:
            self._d._result_cache.pop(key, None)
            return None
        self._d._result_cache.move_to_end(key)
        return result

    def _invalidate_result_cache(self, workspace_root: str) -> None:
        root = os.path.realpath(os.path.abspath(workspace_root))
        for key in list(self._d._result_cache):
            if key[1] == root:
                self._d._result_cache.pop(key, None)
