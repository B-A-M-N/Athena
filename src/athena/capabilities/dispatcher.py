"""CapabilityDispatcher.

The canonical capability invocation lifecycle (BUILDSPEC sections 30-33,
BHV-039..043). Every model-requested action passes through:

    resolve descriptor
      -> validate arguments (BHV-040)
      -> build PolicyRequest
      -> PolicyEngine.evaluate (BHV-041)
      -> if allow: invoke executor, record mutation, return result
      -> if ask:  suspend, signal WAITING_APPROVAL
      -> if deny: failed result with NO effect (BHV-043)

Observable decisions are emitted via an event sink (BHV-042). This is the
single capability path — no bypass (INV-004).
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any

from athena.capabilities.registry import CapabilityRegistry
from athena.concurrency.autonomy import resolve_autonomy_value
from athena.policy.engine import PolicyEngine
from athena.protocol.capabilities import (
    CapabilityRequestOrigin,
    HEALTH_FAILURE_CODES,
    CapabilityFailure,
    CapabilityFailureCode,
    classify_exception_failure,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    DispatchDirectives,
    EffectClass,
    InvocationContext,
)
from athena.protocol.continuations import SuspendedCall
from athena.capabilities.prepared import PreparedCapabilityCall
from athena.capabilities.runtime_escalation import ComplexityFacts, RuntimeEscalation
from athena.capabilities.dispatch_mechanisms import _wrap_exception
from athena.capabilities.dispatch_retry import invoke_with_retry
from athena.capabilities.dispatch_helpers import (
    ReferenceCountedKeyedLocks,
    _cache_ttl,
    _health_state_changed,
    _result_cache_key,
    redact_event_payload as _redact_event_payload,
)
from athena.capabilities.dispatch_mechanisms import (
    _RealityBoundaryDenied,
    DispatchOrdering,
    DispatchProvenance,
    DispatchRepair,
    PolicyGate,
)
from athena.capabilities.dispatch_result_cache import DispatchResultCache
from athena.capabilities.mutation_recorder import MutationRecorder
from athena.protocol.reality import ExecutionDisposition
from athena.protocol.events import EV, make_event
from athena.protocol.ids import new_id
from athena.protocol.policy import (
    DEFAULT_PRINCIPAL_ID,
    PolicyDecision,
    PolicyVerdict,
    Principal,
)
from athena.protocol.tasks import (
    CapabilityPolicy,
    ResourceBudget,
    WorkspaceSpec,
)
from athena.state.approvals import ApprovalStore
from athena.state.mutations import MutationStore


WAITING_APPROVAL = "WAITING_APPROVAL"

_logger = logging.getLogger("athena.capabilities")


class _CapabilityOutputAccumulator:
    """Translate executor output into bounded capability progress facts.

    ExecutionManager already owns stdout/stderr events.  This companion
    signal gives capability-aware consumers a stable lifecycle update without
    duplicating the raw stream or making executors know about the event log.
    """

    def __init__(self, dispatcher, request: CapabilityRequest) -> None:
        self._dispatcher = dispatcher
        self._request = request
        self._bytes = 0

    async def chunk(self, text: str, *, stream: str = "stdout") -> None:
        self._bytes += len(str(text or "").encode("utf-8", errors="replace"))
        await self._dispatcher._emit(
            EV["CAPABILITY_PROGRESS"],
            {
                "call_id": self._request.call_id,
                "capability_id": self._request.capability_id,
                "message": f"{stream}: {self._bytes} bytes observed",
                "stream": stream,
                "bytes_observed": self._bytes,
                "determinate": False,
            },
            self._request.task_id,
            causal_id=self._request.call_id,
        )


class CapabilityDispatcher:
    """Owns the capability invocation lifecycle and the single policy path."""

    _RESULT_CACHE_TTL = 2.0

    def __init__(
        self,
        registry: CapabilityRegistry,
        policy_engine: PolicyEngine,
        *,
        repairer: Any = None,
        candidates=None,
        principal: Principal | None = None,
        mutation_store: MutationStore | None = None,
        artifact_store=None,
        approval_store: ApprovalStore | None = None,
        continuation_store=None,
        repair_store=None,
        event_sink=None,
        mutation_observer=None,
        fabric=None,
        health=None,
        failure_memory=None,
    ) -> None:
        self.registry = registry
        self.policy = policy_engine
        self._principal = principal or Principal("agent", DEFAULT_PRINCIPAL_ID)
        # Inference Compatibility Kernel: deterministic tool-input repair.
        from athena.models.compat.profiles import CompatibilityCandidates
        from athena.models.compat.toolrepair import ToolInputRepairer

        self.repairer = repairer or ToolInputRepairer(
            mode="safe", candidates=candidates or CompatibilityCandidates()
        )
        self._mutation_store = mutation_store
        self._artifact_store = artifact_store
        self._approval_store = approval_store
        self._continuation_store = continuation_store
        self._repair_store = repair_store
        self._event_sink = event_sink
        self._mutation_observer = mutation_observer
        self._fabric = fabric
        self._health = health
        self._failure_memory = failure_memory
        self._budgets = None
        self._suspended: dict[str, SuspendedCall] = {}
        self._resume_expiry: dict[str, datetime] = {}
        self._reality_gate = None
        self._execution_semaphores: dict[str, asyncio.Semaphore] = {}
        self._resource_locks = ReferenceCountedKeyedLocks()
        self._task_held_locks: dict[asyncio.Task, set[asyncio.Lock]] = {}
        self._order_lanes = ReferenceCountedKeyedLocks()
        # Workflow envelopes have their own ordering namespace.  A workflow
        # may await child dispatches, so it must never hold the lane used by
        # those child operations while it is running them.
        self._workflow_lanes = ReferenceCountedKeyedLocks()
        self._runtime_speculative_tasks: set[str] = set()
        self._late_complexity_escalations: set[str] = set()
        self._complexity_ledger: dict[str, ComplexityFacts] = {}
        self._result_cache: OrderedDict[
            tuple[str | None, str, str, str, str | None],
            tuple[float, CapabilityResult],
        ] = OrderedDict()
        self._result_cache_limit = 2048
        self._result_cache_mechanism = DispatchResultCache(self)
        self._event_source: Any = None

    def set_reality_gate(self, gate) -> None:
        """Bind the execution authority that resolves speculative workspaces."""
        self._reality_gate = gate
        bind_runtime_escalations = getattr(gate, "bind_dispatcher", None)
        if callable(bind_runtime_escalations):
            bind_runtime_escalations(self)

    def set_budget_tracker(self, budgets) -> None:
        """Bind the hierarchical execution-budget authority."""
        self._budgets = budgets

    def set_health(self, health) -> None:
        """Bind the runtime health registry used for circuit breaking."""
        self._health = health

    async def reconstruct_complexity_from_events(self, task_id: str) -> dict[str, Any] | None:
        """Restore deterministic task-history complexity from durable events."""
        if not task_id or self._event_source is None:
            return None
        try:
            events = await self._event_source.list_for_task(task_id)
        except Exception as exc:  # noqa: BLE001 - reconstruction is bounded recovery
            _logger.warning("complexity event replay failed for %s: %s", task_id, exc)
            return None
        from athena.capabilities.runtime_escalation import ComplexityLedger

        return ComplexityLedger(self._complexity_ledger).reconstruct_from_events(
            task_id,
            events,
        )

    def set_event_source(self, event_source: Any) -> None:
        """Bind the canonical durable event store for restart reconstruction."""
        self._event_source = event_source

    def release_task_sync(self, task_id: str) -> None:
        """Remove synchronization state for a terminal task.

        Called via the task manager's finalize-observer hook (or explicitly by
        callers that know the task has reached a terminal state). Prevents
        long-running services from accumulating one lane/semaphore per task.
        """
        self._execution_semaphores.pop(task_id, None)
        self._runtime_speculative_tasks.discard(task_id)
        self._late_complexity_escalations.discard(task_id)
        self._complexity_ledger.pop(task_id, None)

    # ------------------------------------------------------------------ #
    # Event emission
    # ------------------------------------------------------------------ #
    async def _emit(
        self,
        type_: str,
        payload: Mapping[str, Any],
        task_id: str | None,
        causal_id: str | None = None,
    ):
        if self._event_sink is None:
            return None
        payload = _redact_event_payload(dict(payload))
        event = make_event(type_, payload, task_id=task_id, causal_id=causal_id)
        await self._event_sink(event)
        return event

    async def emit_progress(
        self,
        *,
        task_id: str | None,
        call_id: str | None,
        capability_id: str,
        value: int,
        total: int,
        unit: str,
        message: str,
    ) -> None:
        """Publish progress only when a component owns a real denominator."""
        if total < 0 or value < 0 or value > total:
            raise ValueError("progress value must be within its total")
        await self._emit(
            EV["CAPABILITY_PROGRESS"],
            {
                "call_id": call_id,
                "capability_id": capability_id,
                "message": message,
                "value": value,
                "total": total,
                "unit": unit,
                "determinate": True,
            },
            task_id,
            causal_id=call_id,
        )

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def dispatch(
        self,
        request: CapabilityRequest,
        *,
        workspace: WorkspaceSpec,
        profile: str | None = None,
        task_policy: CapabilityPolicy | None = None,
        model_policy: Any = None,
        task_budget: ResourceBudget | None = None,
        task_deadline: datetime | None = None,
        runtime_remaining_s: float | None = None,
        verification_environment: Any = None,
        _generated_call_depth: int = 0,
        _generated_call_chain: tuple[str, ...] = (),
        directives: DispatchDirectives | None = None,
        provenance: DispatchProvenance | None = None,
    ) -> CapabilityResult | SuspendedCall:
        """Execute one capability call through the full lifecycle.

        Every public dispatch — direct single-call and batched — passes through
        the same canonical concurrency controls first.  The controls
        deliberately wrap the whole lifecycle, not just executor invocation:
        two same-path writes must not both reach policy/repair concurrently,
        and the execution budget must bound repair/start work too.
        """
        return await self._dispatch_with_controls(
            request,
            workspace=workspace,
            profile=resolve_autonomy_value(profile),
            task_policy=task_policy,
            model_policy=model_policy,
            task_budget=task_budget,
            task_deadline=task_deadline,
            runtime_remaining_s=runtime_remaining_s,
            verification_environment=verification_environment,
            directives=directives,
            batch_order_lock=None,
            provenance=provenance,
        )

    async def _invoke_with_retry(
        self,
        executor,
        request: CapabilityRequest,
        **kwargs,
    ):
        return await invoke_with_retry(self, executor, request, **kwargs)

    def _retry_budget_allows(self, request: CapabilityRequest) -> bool:
        """Compatibility hook for owned budget authority.

        Standalone dispatcher users have no BudgetTracker; service wiring
        passes execution budgets through invocation context, and the second
        attempt remains bounded by max_attempts.
        """
        return True

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
        """Delegate canonical policy evaluation to the policy-gate mechanism."""
        return await PolicyGate(self)._evaluate_call_policy(
            request=request,
            workspace=workspace,
            executor=executor,
            effects=effects,
            profile=profile,
            task_policy=task_policy,
            directives=directives,
        )

    async def _dispatch_prepared_core(
        self,
        prepared: PreparedCapabilityCall | CapabilityRequest,
        *,
        workspace: WorkspaceSpec,
        profile: str | None,
        task_policy: CapabilityPolicy | None,
        model_policy: Any,
        task_budget: ResourceBudget | None,
        task_deadline: datetime | None,
        runtime_remaining_s: float | None,
        verification_environment: Any,
        _generated_call_depth: int = 0,
        _generated_call_chain: tuple[str, ...] = (),
        provenance: DispatchProvenance | None = None,
    ) -> CapabilityResult | SuspendedCall:
        """Execution core: policy, repair, invocation, recording."""
        prepared_call: PreparedCapabilityCall | None
        if isinstance(prepared, PreparedCapabilityCall):
            request = prepared.request
            directives = prepared.directives
            prepared_call = prepared
        else:
            request = prepared
            directives = None
            prepared_call = None
        if provenance is None:
            provenance = DispatchProvenance()
        if not request.call_id:
            object.__setattr__(request, "call_id", new_id("call"))

        await self._emit(
            EV["CAPABILITY_REQUESTED"],
            {
                "call_id": request.call_id,
                "capability_id": request.capability_id,
                "arguments": dict(request.arguments or {}),
            },
            request.task_id,
            causal_id=request.call_id,
        )

        prepared_executor = prepared_call.executor if prepared_call is not None else None
        if prepared_executor is not None:
            executor = prepared_executor
        else:
            executor = self._executor_for(request, workspace)
        self._inject_stores(executor)

        receipt = prepared_call.repair_receipt if prepared_call is not None else None
        effects = await self._resolve_executor_effects(executor, request, workspace)
        combined, reason, policy_failure, inherited_effects = await self._evaluate_call_policy(
            request=request,
            workspace=workspace,
            executor=executor,
            effects=effects,
            profile=profile,
            task_policy=task_policy,
            directives=directives,
        )
        if combined == PolicyVerdict.DENY:
            return await PolicyGate(self)._deny_path(
                request=request,
                policy_failure=policy_failure,
                reason=reason,
            )

        if combined == PolicyVerdict.ASK:
            return await PolicyGate(self)._ask_path(
                request=request,
                workspace=workspace,
                executor=executor,
                effects=effects,
                profile=profile,
                receipt=receipt,
                directives=directives,
                provenance=provenance,
            )

        routed_workspace = workspace
        reality_metadata: dict[str, Any] = {}
        route = None
        if policy_failure is not None:
            await self._emit(
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
            return policy_failure

        try:
            routed_workspace, reality_metadata, route, isolated_call = await PolicyGate(
                self
            )._route_reality(
                request=request,
                workspace=workspace,
                executor=executor,
                effects=effects,
                directives=directives,
            )
        except _RealityBoundaryDenied as boundary:
            return boundary.result

        cache_key = await _result_cache_key(
            request,
            routed_workspace,
            effects,
            profile=profile,
            descriptor=executor.descriptor,
        )
        try:
            # A speculative branch may mutate between verification probes.
            # Never replay a cached observation while certification is active.
            verification_call = (
                getattr(request.origin, "value", request.origin)
                == CapabilityRequestOrigin.SYSTEM_VERIFICATION.value
            )
            if cache_key is not None and not isolated_call and not verification_call:
                cached = self._result_cache_mechanism.cached_result(cache_key)
                if cached is not None:
                    await self._emit(
                        EV["CAPABILITY_STARTED"],
                        {
                            "call_id": request.call_id,
                            "capability_id": request.capability_id,
                            "cache": "hit",
                        },
                        request.task_id,
                        causal_id=request.call_id,
                    )
                    result = replace(
                        cached,
                        call_id=request.call_id,
                        metadata={**dict(cached.metadata), "cache_hit": True},
                    )
                    if reality_metadata:
                        metadata = dict(result.metadata or {})
                        metadata["reality"] = reality_metadata
                        object.__setattr__(result, "metadata", metadata)
                    await self._result_cache_mechanism.emit_result_observations(request, result)
                    await self._emit(
                        EV["CAPABILITY_COMPLETED"],
                        {
                            "call_id": request.call_id,
                            "capability_id": request.capability_id,
                            "cache": "hit",
                        },
                        request.task_id,
                        causal_id=request.call_id,
                    )
                    return result

            await self._emit(
                EV["CAPABILITY_STARTED"],
                {
                    "call_id": request.call_id,
                    "capability_id": request.capability_id,
                },
                request.task_id,
                causal_id=request.call_id,
            )

            effective_directives = (
                replace(
                    directives or DispatchDirectives(),
                    inherited_effects=frozenset(effects),
                    inherited_capability_id=request.capability_id,
                )
                if combined == PolicyVerdict.ALLOW
                else (directives or DispatchDirectives())
            )
            context = InvocationContext(
                task_id=request.task_id,
                principal_id=self._principal.id,
                workspace=routed_workspace,
                execution_backend=routed_workspace.execution_backend or "local",
                capability_policy=task_policy,
                model_policy=model_policy,
                resource_budget=task_budget,
                deadline=task_deadline,
                runtime_remaining_s=runtime_remaining_s,
                verification_environment=verification_environment,
                autonomy=profile,
                generated_call_depth=_generated_call_depth,
                generated_call_chain=tuple(_generated_call_chain),
                directives=effective_directives,
            )
            try:
                result = await self._invoke_with_retry(
                    executor,
                    request,
                    output_accumulator=_CapabilityOutputAccumulator(self, request),
                    context=context,
                )
                # WorkflowCapability can surface the exact inner suspended
                # call so the kernel parks the real approval continuation.
                # It is already persisted by _park_for_approval; wrapping it
                # as a failed outer result would lose the workflow's resume
                # semantics.
                if isinstance(result, SuspendedCall):
                    return result
            except (KeyboardInterrupt, SystemExit):
                raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failure = classify_exception_failure(
                    exc,
                    descriptor=getattr(executor, "descriptor", None),
                    operation=str((request.arguments or {}).get("operation") or ""),
                    resource=str((request.arguments or {}).get("path") or ""),
                )
                if self._health is not None and failure.code in HEALTH_FAILURE_CODES:
                    before_health = self._health.get(request.capability_id)
                    health_record = self._health.record_failure(request.capability_id, str(exc))
                    state_changed = _health_state_changed(before_health, health_record)
                    if self._result_cache_mechanism.health_should_persist(
                        request.capability_id, state_changed
                    ):
                        await self._result_cache_mechanism.persist_health(request.capability_id)
                    if state_changed:
                        await self._emit(
                            "CapabilityHealthChanged",
                            {
                                "capability_id": request.capability_id,
                                "health": health_record,
                            },
                            request.task_id,
                            causal_id=request.call_id,
                        )
                raise
            result_failure: CapabilityFailure | None = None
            if result.status is not CapabilityResultStatus.OK:
                result_failure = CapabilityFailure.from_metadata(result.metadata or {})
                if result_failure is None and result.error:
                    result_failure = classify_exception_failure(
                        ValueError(result.error),
                        descriptor=getattr(executor, "descriptor", None),
                        operation=str((request.arguments or {}).get("operation") or ""),
                        resource=str((request.arguments or {}).get("path") or ""),
                    )
                if request.task_id:
                    from athena.capabilities.runtime_escalation import ComplexityLedger

                    ledger = ComplexityLedger(self._complexity_ledger)
                    if request.origin is CapabilityRequestOrigin.SYSTEM_VERIFICATION:
                        ledger.record_candidate_verification_failure(request.task_id)
                        # A failed candidate verification is a strong runtime
                        # complexity signal: route the task through a sticky
                        # candidate unless it already has one.
                        if self._reality_gate is not None:
                            active = getattr(self._reality_gate, "active_branch", None)
                            if not (callable(active) and active(request.task_id) is not None):
                                self._runtime_speculative_tasks.add(request.task_id)
                                self._late_complexity_escalations.add(request.task_id)
                    ledger.record_capability_failure(
                        request.task_id,
                        infrastructure=(
                            result_failure is not None
                            and result_failure.code in HEALTH_FAILURE_CODES
                        ),
                        candidate_attempt=(
                            request.origin is CapabilityRequestOrigin.SYSTEM_VERIFICATION
                        ),
                    )
                is_infrastructure = (
                    result_failure is not None and result_failure.code in HEALTH_FAILURE_CODES
                )
                is_domain = (
                    result_failure is not None
                    and result_failure.code is not CapabilityFailureCode.INVALID_INPUT
                    and not is_infrastructure
                )
            else:
                is_domain = False
            if self._health is not None:
                before_health = self._health.get(request.capability_id)
                if result.status is CapabilityResultStatus.OK:
                    health = self._health.record_success(request.capability_id)
                elif is_infrastructure:
                    health = self._health.record_failure(request.capability_id, result.error)
                elif is_domain:
                    health = self._health.record_domain_outcome(request.capability_id, result.error)
                else:
                    # Unclassified failures remain failures in health rather
                    # than pretending the capability call succeeded.
                    health = self._health.record_failure(request.capability_id, result.error)
                state_changed = _health_state_changed(before_health, health)
                if self._result_cache_mechanism.health_should_persist(
                    request.capability_id, state_changed
                ):
                    await self._result_cache_mechanism.persist_health(request.capability_id)
                if state_changed:
                    await self._emit(
                        "CapabilityHealthChanged",
                        {
                            "capability_id": request.capability_id,
                            "health": health,
                        },
                        request.task_id,
                        causal_id=request.call_id,
                    )
            if reality_metadata:
                metadata = dict(result.metadata or {})
                metadata["reality"] = reality_metadata
                object.__setattr__(result, "metadata", metadata)
            # Canonical receipt: stamp the resolved effects and execution
            # identity onto the result metadata so downstream evidence
            # classification can consume the dispatcher's authoritative
            # resolution instead of reverse-engineering from names.
            if "resolved_effects" not in (result.metadata or {}):
                receipt_meta = dict(result.metadata or {})
                receipt_meta["resolved_effects"] = sorted(effect.value for effect in effects)
                if execution_backend := getattr(routed_workspace, "execution_backend", None):
                    receipt_meta["execution_backend"] = execution_backend
                if route is not None and route.disposition is not None:
                    receipt_meta["reality_disposition"] = route.disposition.value
                object.__setattr__(result, "metadata", receipt_meta)
            await self._result_cache_mechanism.attach_failure_memory(request, result, workspace)
            if result.status == CapabilityResultStatus.OK:
                instrument = (result.metadata or {}).get("instrument")
                if isinstance(instrument, Mapping):
                    from athena.protocol.instruments import InstrumentView

                    try:
                        view = InstrumentView.from_record(instrument)
                    except (TypeError, ValueError):
                        view = None
                    if view is not None:
                        await self._emit(
                            EV["INSTRUMENT_PRODUCED"],
                            {
                                "call_id": request.call_id,
                                "capability_id": request.capability_id,
                                "instrument": view.to_record(),
                            },
                            request.task_id,
                            causal_id=request.call_id,
                        )
                mutation = (result.metadata or {}).get("mutation")
                if mutation:
                    await self._emit(
                        EV["MUTATION_PREPARED"],
                        {
                            "call_id": request.call_id,
                            "capability_id": request.capability_id,
                            "resource": mutation.get("resource"),
                            "operation": mutation.get("operation"),
                            "mutation_id": mutation.get("mutation_id"),
                        },
                        request.task_id,
                        causal_id=request.call_id,
                    )
                await self._result_cache_mechanism.emit_result_observations(request, result)
                # An isolated branch is intentionally discarded, so recording
                # its filesystem mutation as a real mutation would be false.
                if not isolated_call and not verification_call:
                    await self._record_mutation(request, result)
                if (
                    route is not None
                    and route.disposition is ExecutionDisposition.TRANSACTIONAL
                    and (result.metadata or {}).get("mutation")
                    and self._reality_gate is not None
                ):
                    mutation = (result.metadata or {}).get("mutation") or {}
                    await self._reality_gate.note_transaction_progress(
                        request.task_id,
                        routed_workspace.root,
                        mutation=True,
                        mutation_id=mutation.get("mutation_id"),
                        resource=mutation.get("resource"),
                    )
                if not isolated_call and not verification_call and cache_key is not None:
                    self._result_cache_mechanism.store_success(
                        cache_key, result, executor.descriptor, request.arguments or {}
                    )
                elif not isolated_call and not verification_call:
                    self._result_cache_mechanism.invalidate(routed_workspace.root)
                await self._emit(
                    EV["CAPABILITY_COMPLETED"],
                    {
                        "call_id": request.call_id,
                        "capability_id": request.capability_id,
                    },
                    request.task_id,
                    causal_id=request.call_id,
                )
            else:
                await self._result_cache_mechanism.emit_result_observations(request, result)
                await self._emit(
                    EV["CAPABILITY_FAILED"],
                    {
                        "call_id": request.call_id,
                        "capability_id": request.capability_id,
                        "reason": result.error,
                    },
                    request.task_id,
                    causal_id=request.call_id,
                )
            return result
        finally:
            if isolated_call and self._reality_gate is not None:
                await self._reality_gate.discard_ephemeral(request.call_id)

    async def dispatch_many(
        self,
        requests: list[CapabilityRequest],
        *,
        workspace: WorkspaceSpec,
        profile: str | None = None,
        task_policy: CapabilityPolicy | None = None,
        model_policy: Any = None,
        task_budget: ResourceBudget | None = None,
        task_deadline: datetime | None = None,
        runtime_remaining_s: float | None = None,
        verification_environment: Any = None,
        preflight: bool = True,
        directives_by_call_id: Mapping[str, DispatchDirectives] | None = None,
        provenance: DispatchProvenance | None = None,
    ) -> list[CapabilityResult | SuspendedCall]:
        """Dispatch multiple independent capability calls in parallel (BHV-041).

        With ``preflight=True`` (default), ALL calls are validated/repaired
        FIRST and nothing executes unless every call is well-formed: resolve
        executor -> deterministic repair -> schema validation -> effect
        resolution. Any unrepairable-invalid or unknown-capability call aborts
        the whole batch with a single failed result naming each issue path
        (review item 69) — a partial execution of a model-produced batch must
        never happen.
        """
        if not requests:
            return []

        if provenance is None:
            provenance = DispatchProvenance(repair_mode="safe")

        profile = resolve_autonomy_value(profile)
        batch_lock = asyncio.Lock()
        if preflight:
            prepared_calls = await self._preflight_batch(
                requests,
                workspace=workspace,
                provenance=provenance,
                directives_by_call_id=directives_by_call_id,
            )
            self._escalate_complex_prepared_batch(prepared_calls)
            issues = DispatchRepair.preflight_issues(prepared_calls)
            if issues:
                first = requests[0]
                result = CapabilityResult(
                    first.call_id or new_id("call"),
                    first.capability_id,
                    CapabilityResultStatus.FAILED,
                    error="batch_preflight_failed: " + "; ".join(issues),
                    metadata={"preflight_issues": issues},
                )
                await self._emit(
                    EV["CAPABILITY_FAILED"],
                    {
                        "call_id": result.call_id,
                        "capability_id": first.capability_id,
                        "reason": "batch_preflight_failed",
                        "error": result.error,
                    },
                    first.task_id,
                    causal_id=result.call_id,
                )
                return [result]

            results = await asyncio.gather(
                *[
                    self._dispatch_with_controls(
                        prepared,
                        profile=profile,
                        task_policy=task_policy,
                        model_policy=model_policy,
                        task_budget=task_budget,
                        task_deadline=task_deadline,
                        runtime_remaining_s=runtime_remaining_s,
                        verification_environment=verification_environment,
                        batch_order_lock=batch_lock,
                        provenance=provenance,
                    )
                    for prepared in prepared_calls
                ],
                return_exceptions=True,
            )
        else:
            results = await asyncio.gather(
                *[
                    self._dispatch_with_controls(
                        request,
                        workspace=workspace,
                        profile=profile,
                        task_policy=task_policy,
                        model_policy=model_policy,
                        task_budget=task_budget,
                        task_deadline=task_deadline,
                        runtime_remaining_s=runtime_remaining_s,
                        verification_environment=verification_environment,
                        directives=(directives_by_call_id or {}).get(request.call_id),
                        batch_order_lock=batch_lock,
                        provenance=provenance,
                    )
                    for request in requests
                ],
                return_exceptions=True,
            )
        return [
            _wrap_exception(r, requests[i]) if isinstance(r, BaseException) else r
            for i, r in enumerate(results)
        ]

    async def prepare_one(
        self,
        request: CapabilityRequest,
        workspace: WorkspaceSpec,
        *,
        provenance: DispatchProvenance | None = None,
        directives: DispatchDirectives | None = None,
    ) -> PreparedCapabilityCall:
        """Canonical preparation for direct, batched, and resume dispatch."""
        return await DispatchRepair(self).prepare_one(
            replace(request, call_id=request.call_id or new_id("call")),
            workspace,
            provenance=provenance,
            directives=directives,
        )

    def _batch_order(self, request, workspace, effects, batch_order_lock):
        return DispatchOrdering(self)._batch_order(request, workspace, effects, batch_order_lock)

    async def _dispatch_with_controls(self, prepared, **kwargs):
        return await DispatchOrdering(self)._dispatch_with_controls(prepared, **kwargs)

    def _locks_for_request(self, request, workspace, effects, *, executor=None):
        return DispatchOrdering(self)._locks_for_request(
            request, workspace, effects, executor=executor
        )

    def _release_locks(self, locks):
        return DispatchOrdering(self)._release_locks(locks)

    def _executor_for(self, request, workspace):
        return DispatchOrdering(self)._executor_for(request, workspace)

    async def _preflight_batch(
        self, requests, *, workspace, provenance=None, directives_by_call_id=None
    ):
        return await DispatchRepair(self)._preflight_batch(
            requests,
            workspace=workspace,
            provenance=provenance,
            directives_by_call_id=directives_by_call_id,
        )

    def _escalate_complex_prepared_batch(self, prepared_calls):
        return RuntimeEscalation(self)._escalate_complex_prepared_batch(prepared_calls)

    async def _persist_repair(
        self,
        request: CapabilityRequest,
        receipt,
        *,
        original_arguments: Any,
        canonical_arguments: Mapping[str, Any] | None,
    ) -> None:
        return await DispatchRepair(self)._persist_repair(
            request,
            receipt,
            original_arguments=original_arguments,
            canonical_arguments=canonical_arguments,
        )

    async def _emit_repair(self, request: CapabilityRequest, receipt, canonical_arguments) -> None:
        return await DispatchRepair(self)._emit_repair(request, receipt, canonical_arguments)

    # ------------------------------------------------------------------ #
    # Resolution / mutation
    # ------------------------------------------------------------------ #
    # Capabilities whose operations are process/code operations, not file
    # writes — even when an op name like "create" would suggest a write.
    _EXEC_CAPABILITIES = frozenset(
        {"execute", "terminal_session", "process", "debugger", "shell", "bash"}
    )

    def resolve_effects(self, request: CapabilityRequest, workspace: WorkspaceSpec):
        """Public read-only effect resolution for kernel replay contracts."""
        executor = self._executor_for(request, workspace)
        return self._resolve_effects_for(executor.descriptor, request.arguments or {})

    async def _resolve_executor_effects(
        self, executor: Any, request: CapabilityRequest, workspace: WorkspaceSpec
    ) -> tuple[EffectClass, ...]:
        resolver = getattr(executor, "resolve_operation_effects", None)
        if callable(resolver):
            return tuple(
                await resolver(
                    request.arguments or {},
                    workspace=workspace,
                    task_id=request.task_id,
                    principal_id=self._principal.id,
                )
            )
        return self._resolve_effects_for(executor.descriptor, request.arguments or {})

    @staticmethod
    def _resolve_effects_for(descriptor, arguments: Mapping[str, Any]) -> tuple[EffectClass, ...]:
        return PolicyGate._resolve_effects_for(descriptor, arguments)

    @staticmethod
    def _resolve_effects(descriptor, arguments: Mapping[str, Any]) -> tuple[EffectClass, ...]:
        return PolicyGate._resolve_effects(descriptor, arguments)

    @staticmethod
    def _eval_task_policy(
        capability_id: str,
        task_policy: CapabilityPolicy | None,
        request_effects: frozenset[EffectClass] | None = None,
    ) -> PolicyVerdict | None:
        return PolicyGate._eval_task_policy(capability_id, task_policy, request_effects)

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
        return await PolicyGate(self)._park_for_approval(
            request,
            decision=decision,
            workspace=workspace,
            arguments=arguments,
            effects=effects,
            receipt=receipt,
            directives=directives,
            provenance=provenance,
        )

    def _inject_stores(self, executor) -> None:
        """Hand the dispatcher's durable stores to mutating executors.

        Filesystem executors capture before-state snapshots and write-ahead
        intents themselves; they need the same stores the dispatcher owns so the
        ledger is a single, consistent record regardless of the executor path.
        Attribute assignment is duck-typed: executors without these attributes
        stay untouched.
        """
        if self._mutation_store is not None and not getattr(executor, "mutation_store", None):
            try:
                executor.mutation_store = self._mutation_store
            except AttributeError:
                pass
        if self._artifact_store is not None and not getattr(executor, "artifact_store", None):
            try:
                executor.artifact_store = self._artifact_store
            except AttributeError:
                pass

    # Mutation bookkeeping mechanism (P1-10): bodies live in
    # :mod:`athena.capabilities.mutation_recorder`. The dispatcher keeps the
    # entrypoints and remains the only caller that decides WHEN recording runs.

    async def _record_mutation(
        self,
        request: CapabilityRequest,
        result: CapabilityResult,
    ) -> None:
        return await MutationRecorder(self)._record_mutation(request, result)

    @staticmethod
    async def _mutation_sequence_for(store, mutation_id: str) -> int | None:
        return await MutationRecorder._mutation_sequence_for(store, mutation_id)

    @staticmethod
    def _attach_mutation_boundary(
        result: CapabilityResult,
        *,
        event_sequence: int | None,
        mutation_sequence: int | None,
    ) -> None:
        return MutationRecorder._attach_mutation_boundary(
            result, event_sequence=event_sequence, mutation_sequence=mutation_sequence
        )
