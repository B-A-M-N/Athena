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
import json
import os
import re
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta
import time
from typing import Any

from athena.capabilities.registry import CapabilityRegistry, validate_schema
from athena.policy.approvals import args_digest
from athena.policy.engine import PolicyEngine
from athena.protocol.capabilities import (
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    CachePolicy,
    DispatchDirectives,
    EffectClass,
    ExternalEffectPhase,
    InvocationContext,
)
from athena.protocol.errors import CapabilityUnavailable, PersistenceError
from athena.reality.gate import ExecutionDisposition
from athena.protocol.events import EV, make_event
from athena.protocol.ids import new_id
from athena.protocol.policy import (
    ApprovalScope,
    PolicyDecision,
    PolicyRequest,
    PolicyVerdict,
    Principal,
)
from athena.protocol.tasks import CapabilityPolicy, ResourceBudget, WorkspaceSpec
from athena.state.approvals import ApprovalStore
from athena.state.mutations import MutationStore


class SuspendedCall:
    """A capability call parked on an ``ask`` decision, awaiting approval."""

    def __init__(
        self,
        call_id: str,
        request: CapabilityRequest,
        decision: PolicyDecision,
        approval_id: str | None = None,
        directives: DispatchDirectives | None = None,
    ) -> None:
        self.call_id = call_id
        self.request = request
        self.decision = decision
        self.approval_id = approval_id
        self.directives = directives
        # Filled by WorkflowCapability for same-process resume.  The durable
        # directive fields carry the equivalent identity across restart.
        self.workflow_run_id: str | None = None
        self.workflow_id: str | None = None
        self.workflow_parent_request: CapabilityRequest | None = None


WAITING_APPROVAL = "WAITING_APPROVAL"


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
        self._principal = principal or Principal("agent", "athena")
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
        self._provider_profile_id: str | None = None
        self._model_id: str | None = None
        self._repair_mode: str | None = None
        self._reality_gate = None
        self._execution_semaphores: dict[str, asyncio.Semaphore] = {}
        self._resource_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._result_cache: OrderedDict[
            tuple[str | None, str, str, str, str | None],
            tuple[float, CapabilityResult],
        ] = OrderedDict()
        self._result_cache_limit = 2048

    def set_reality_gate(self, gate) -> None:
        """Bind the execution authority that resolves speculative workspaces."""
        self._reality_gate = gate

    def set_budget_tracker(self, budgets) -> None:
        """Bind the hierarchical execution-budget authority."""
        self._budgets = budgets

    def set_health(self, health) -> None:
        """Bind the runtime health registry used for circuit breaking."""
        self._health = health

    def set_inference_provenance(
        self,
        *,
        provider_profile_id: str | None,
        model_id: str | None,
        repair_mode: str | None = None,
    ) -> None:
        """Bind the current model turn to repair receipts and audit events."""
        self._provider_profile_id = provider_profile_id
        self._model_id = model_id
        self._repair_mode = repair_mode

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
        task_budget: ResourceBudget | None = None,
        task_deadline: datetime | None = None,
        runtime_remaining_s: float | None = None,
        verification_environment: Any = None,
        _generated_call_depth: int = 0,
        _generated_call_chain: tuple[str, ...] = (),
        _directives: DispatchDirectives | None = None,
        _prepared: bool = False,
    ) -> CapabilityResult | SuspendedCall:
        """Execute one capability call through the full lifecycle.

        ``task_policy`` (the calling task's CapabilityPolicy) is enforced as a
        HARD ceiling BEFORE the global/profile policy: denote it a hard deny, and
        global policy can only narrow further, never expand task authority.
        """
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

        executor = self._executor_for(request, workspace)
        self._inject_stores(executor)

        # Inference Compatibility Kernel: deterministic repair BEFORE policy.
        # Model-produced calls get one bounded schema-directed repair pass;
        # strict revalidation follows; policy/approval/execution all see the
        # exact canonical arguments. (Spec invariant: repair changes syntax
        # and representation only — never capability or trust.)
        # If the provider boundary recorded a RAW unparseable arguments string
        # for this call, give the repairer the original bytes so rules like
        # double-decode / control-char escape can act on them.
        from athena.protocol.capabilities import CapabilityRequestOrigin

        if _prepared:
            # dispatch_many() already repaired and validated every request.
            # Re-running repair here would make a batch depend on mutable
            # repair policy and could produce a different canonical call.
            repaired_args = request.arguments
            receipt = None
        else:
            from athena.models.compat.candidates import get_raw_candidate

            repair_arguments = dict(request.arguments or {})
            candidate = request.candidate or get_raw_candidate(request.call_id)
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
            else:
                completion_state = "CLEAN"

            origin_value = getattr(request.origin, "value", request.origin)
            if origin_value == CapabilityRequestOrigin.MODEL.value:
                repaired_args, receipt = self.repairer.repair(
                    call_id=request.call_id,
                    tool_name=request.capability_id,
                    arguments=repair_arguments,
                    input_schema=executor.descriptor.input_schema,
                    validate_fn=validate_schema,
                    mcp_origin=(
                        origin_value == CapabilityRequestOrigin.MCP.value
                        or getattr(executor.descriptor.origin, "value", None) == "MCP"
                    ),
                    provider_profile_id=(
                        getattr(candidate, "provider_profile_id", None) or self._provider_profile_id
                    ),
                    model_id=getattr(candidate, "model_id", None) or self._model_id,
                    completion_state=completion_state,
                    mode=self._repair_mode,
                )
            else:
                # User/system/orchestrator/MCP calls are internal protocol input,
                # not model compatibility input. Invalid internal arguments are
                # a hard failure and must never be silently repaired.
                internal_errors = validate_schema(
                    executor.descriptor.input_schema, request.arguments or {}
                )
                repaired_args = request.arguments
                receipt = None
                if internal_errors:
                    result = CapabilityResult(
                        request.call_id,
                        request.capability_id,
                        CapabilityResultStatus.FAILED,
                        error="tool_input_invalid: " + "; ".join(internal_errors),
                        metadata={"origin": getattr(request.origin, "value", request.origin)},
                    )
                    await self._emit(
                        EV["CAPABILITY_FAILED"],
                        {
                            "call_id": request.call_id,
                            "capability_id": request.capability_id,
                            "reason": "tool_input_invalid",
                            "error": result.error,
                        },
                        request.task_id,
                        causal_id=request.call_id,
                    )
                    return result
        if receipt is not None:
            await self._persist_repair(
                request,
                receipt,
                original_arguments=repair_arguments,
                canonical_arguments=(repaired_args if receipt.outcome != "INVALID" else None),
            )
        if receipt is not None and receipt.outcome == "INVALID":
            result = CapabilityResult(
                request.call_id,
                request.capability_id,
                CapabilityResultStatus.FAILED,
                error="tool_input_invalid: "
                + ("; ".join(receipt.issue_codes) or "invalid arguments"),
                metadata={"repair": receipt.to_dict()},
            )
            await self._emit(
                EV["CAPABILITY_FAILED"],
                {
                    "call_id": request.call_id,
                    "capability_id": request.capability_id,
                    "reason": "tool_input_invalid",
                    "error": result.error,
                },
                request.task_id,
                causal_id=request.call_id,
            )
            return result
        if receipt is not None and receipt.outcome == "REPAIRED":
            object.__setattr__(request, "arguments", repaired_args)
            await self._emit_repair(request, receipt, repaired_args)

        errors = validate_schema(executor.descriptor.input_schema, request.arguments or {})
        if errors:
            result = CapabilityResult(
                request.call_id,
                request.capability_id,
                CapabilityResultStatus.FAILED,
                error="validation failed: " + "; ".join(errors),
            )
            await self._emit(
                EV["CAPABILITY_FAILED"],
                {
                    "call_id": request.call_id,
                    "capability_id": request.capability_id,
                    "reason": "schema_validation",
                    "error": result.error,
                },
                request.task_id,
                causal_id=request.call_id,
            )
            return result

        await self._emit(
            EV["CAPABILITY_VALIDATED"],
            {
                "call_id": request.call_id,
                "capability_id": request.capability_id,
            },
            request.task_id,
            causal_id=request.call_id,
        )

        try:
            effects = self._resolve_effects_for(executor.descriptor, request.arguments or {})
        except ValueError as exc:
            result = CapabilityResult(
                request.call_id,
                request.capability_id,
                CapabilityResultStatus.FAILED,
                error=f"invalid operation effects: {exc}",
            )
            await self._emit(
                EV["CAPABILITY_FAILED"],
                {
                    "call_id": request.call_id,
                    "capability_id": request.capability_id,
                    "reason": "effects_unresolved",
                    "error": result.error,
                },
                request.task_id,
                causal_id=request.call_id,
            )
            return result
        inherited_effects = frozenset(getattr(_directives, "inherited_effects", ()))
        if inherited_effects and not set(effects).issubset(inherited_effects):
            result = CapabilityResult(
                request.call_id,
                request.capability_id,
                CapabilityResultStatus.FAILED,
                error=("generated host call requires effects outside its parent authority ceiling"),
                metadata={
                    "decision": "deny",
                    "reason": "inherited_effect_ceiling",
                    "inherited_effects": sorted(effect.value for effect in inherited_effects),
                    "requested_effects": [effect.value for effect in effects],
                },
            )
            await self._emit(
                EV["CAPABILITY_FAILED"],
                {
                    "call_id": request.call_id,
                    "capability_id": request.capability_id,
                    "reason": "inherited_effect_ceiling",
                    "error": result.error,
                },
                request.task_id,
                causal_id=request.call_id,
            )
            return result
        policy_request = PolicyRequest(
            principal=self._principal,
            task_id=request.task_id,
            capability_id=request.capability_id,
            arguments=dict(request.arguments or {}),
            workspace=workspace,
            execution_backend=workspace.execution_backend or "local",
            effects=frozenset(effects),
            session_id=getattr(request, "session_id", None),
            call_id=request.call_id,
        )
        decision = self.policy.evaluate(policy_request, autonomy=profile)
        global_verdict = _verdict(decision.decision)

        task_verdict = self._eval_task_policy(
            request.capability_id, task_policy, request_effects=frozenset(effects)
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
        orchestration_approval_id = _directives.approval_id if _directives is not None else None
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
                f"{getattr(_directives, 'inherited_capability_id', None) or 'call'}"
            )
        await self._emit(
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

        if combined == PolicyVerdict.DENY:
            if global_verdict == PolicyVerdict.DENY:
                error = f"denied: {decision.reason}"
            else:
                error = f"denied: task capability policy forbids {request.capability_id}"
            await self._emit(
                EV["CAPABILITY_FAILED"],
                {
                    "call_id": request.call_id,
                    "reason": "denied",
                    "matched_rule": decision.matched_rule,
                },
                request.task_id,
                causal_id=request.call_id,
            )
            return CapabilityResult(
                request.call_id,
                request.capability_id,
                CapabilityResultStatus.FAILED,
                error=error,
                metadata={"decision": "deny", "matched_rule": decision.matched_rule},
            )

        if combined == PolicyVerdict.ASK:
            approval_id = await self._park_for_approval(
                request,
                decision=decision,
                workspace=workspace,
                arguments=dict(request.arguments or {}),
                effects=effects,
                receipt=receipt,
                directives=_directives,
            )
            suspended = SuspendedCall(
                request.call_id,
                request,
                decision,
                approval_id,
                _directives,
            )
            self._suspended[request.call_id] = suspended
            await self._emit(
                EV["APPROVAL_REQUESTED"],
                {
                    "call_id": request.call_id,
                    "capability_id": request.capability_id,
                    "task_id": request.task_id,
                    "approval_id": approval_id,
                    "scopes": [s.value for s in decision.approval_scope_options],
                },
                request.task_id,
                causal_id=request.call_id,
            )
            return suspended

        if self._health is not None:
            healthy, health_record = self._health.before_call(request.capability_id)
            if not healthy:
                result = CapabilityResult(
                    request.call_id,
                    request.capability_id,
                    CapabilityResultStatus.FAILED,
                    error=(
                        f"capability circuit is open; retry after "
                        f"{health_record.get('retry_after_seconds', 0)} seconds"
                    ),
                    metadata={"health": health_record, "decision": "circuit_open"},
                )
                await self._emit(
                    EV["CAPABILITY_FAILED"],
                    {
                        "call_id": request.call_id,
                        "capability_id": request.capability_id,
                        "reason": "circuit_open",
                        "retry_after_seconds": health_record.get("retry_after_seconds", 0),
                    },
                    request.task_id,
                    causal_id=request.call_id,
                )
                return result

        routed_workspace = workspace
        reality_metadata: dict[str, Any] = {}
        route = None
        if self._reality_gate is not None:
            try:
                route = await self._reality_gate.route(
                    request,
                    workspace,
                    frozenset(effects),
                    executor.descriptor,
                    tier=(_directives.reality_tier if _directives is not None else None),
                )
            except PermissionError as exc:
                result = CapabilityResult(
                    request.call_id,
                    request.capability_id,
                    CapabilityResultStatus.FAILED,
                    error=str(exc),
                    metadata={"decision": "reality_boundary"},
                )
                await self._emit(
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
                return result
            routed_workspace = route.workspace
            reality_metadata = route.metadata()

        isolated_call = route is not None and route.disposition is ExecutionDisposition.ISOLATED
        verification_call = routed_workspace.execution_backend == "verification"

        cache_key = await _result_cache_key(
            request,
            routed_workspace,
            effects,
            profile=profile,
            descriptor=executor.descriptor,
        )
        try:
            if cache_key is not None and not isolated_call:
                cached = self._cached_result(cache_key)
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
                    await self._emit_result_observations(request, result)
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
                    _directives or DispatchDirectives(),
                    inherited_effects=frozenset(effects),
                    inherited_capability_id=request.capability_id,
                )
                if combined == PolicyVerdict.ALLOW
                else (_directives or DispatchDirectives())
            )
            context = InvocationContext(
                task_id=request.task_id,
                workspace=routed_workspace,
                execution_backend=routed_workspace.execution_backend or "local",
                capability_policy=task_policy,
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
                result = await executor.invoke(
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
            except Exception as exc:
                if self._health is not None:
                    before_health = self._health.get(request.capability_id)
                    health_record = self._health.record_failure(request.capability_id, str(exc))
                    state_changed = _health_state_changed(before_health, health_record)
                    if self._health_should_persist(request.capability_id, state_changed):
                        await self._persist_health(request.capability_id)
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
            if self._health is not None:
                before_health = self._health.get(request.capability_id)
                health = (
                    self._health.record_success(request.capability_id)
                    if result.status is CapabilityResultStatus.OK
                    else self._health.record_failure(request.capability_id, result.error)
                )
                state_changed = _health_state_changed(before_health, health)
                if self._health_should_persist(request.capability_id, state_changed):
                    await self._persist_health(request.capability_id)
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
            await self._attach_failure_memory(request, result, workspace)
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
                await self._emit_result_observations(request, result)
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
                    if cache_key is not None:
                        self._result_cache[cache_key] = (
                            time.monotonic()
                            + _cache_ttl(
                                executor.descriptor,
                                executor.descriptor.resolve_cache_policy(request.arguments or {}),
                            ),
                            result,
                        )
                        self._result_cache.move_to_end(cache_key)
                        while len(self._result_cache) > self._result_cache_limit:
                            self._result_cache.popitem(last=False)
                elif not isolated_call and not verification_call:
                    self._invalidate_result_cache(routed_workspace.root)
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
                await self._emit_result_observations(request, result)
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

    async def _persist_health(self, capability_id: str) -> None:
        persist = getattr(self._health, "persist", None)
        if persist is None:
            return
        try:
            await persist(capability_id)
            mark_persisted = getattr(self._health, "mark_persisted", None)
            if callable(mark_persisted):
                mark_persisted(capability_id)
        except Exception as exc:  # health persistence must not alter call truth
            await self._emit(
                "CapabilityHealthChanged",
                {
                    "capability_id": capability_id,
                    "persistence_error": str(exc)[:500],
                },
                None,
                causal_id=capability_id,
            )

    def _health_should_persist(self, capability_id: str, state_changed: bool) -> bool:
        should_persist = getattr(self._health, "should_persist", None)
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
        await self._emit(
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
        if self._failure_memory is None or result.status is CapabilityResultStatus.OK:
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
                    await self._failure_memory.retrieve(
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
        entry = self._result_cache.get(key)
        if entry is None:
            return None
        expires_at, result = entry
        if time.monotonic() >= expires_at:
            self._result_cache.pop(key, None)
            return None
        self._result_cache.move_to_end(key)
        return result

    def _invalidate_result_cache(self, workspace_root: str) -> None:
        root = os.path.realpath(os.path.abspath(workspace_root))
        for key in list(self._result_cache):
            if key[1] == root:
                self._result_cache.pop(key, None)

    async def dispatch_many(
        self,
        requests: list[CapabilityRequest],
        *,
        workspace: WorkspaceSpec,
        profile: str | None = None,
        task_policy: CapabilityPolicy | None = None,
        task_budget: ResourceBudget | None = None,
        task_deadline: datetime | None = None,
        runtime_remaining_s: float | None = None,
        verification_environment: Any = None,
        preflight: bool = True,
        _directives_by_call_id: Mapping[str, DispatchDirectives] | None = None,
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

        if preflight:
            issues = await self._preflight_batch(requests, workspace=workspace)
            if issues:
                first = requests[0]
                paths = "; ".join(issues)
                result = CapabilityResult(
                    first.call_id or new_id("call"),
                    first.capability_id,
                    CapabilityResultStatus.FAILED,
                    error="batch_preflight_failed: " + paths,
                    metadata={"preflight_issues": list(issues)},
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
                    r,
                    workspace=workspace,
                    profile=profile,
                    task_policy=task_policy,
                    task_budget=task_budget,
                    task_deadline=task_deadline,
                    runtime_remaining_s=runtime_remaining_s,
                    verification_environment=verification_environment,
                    directives=(_directives_by_call_id or {}).get(r.call_id),
                    prepared=preflight,
                )
                for r in requests
            ],
            return_exceptions=True,
        )
        return [
            _wrap_exception(r, requests[i]) if isinstance(r, BaseException) else r
            for i, r in enumerate(results)
        ]

    async def _dispatch_with_controls(
        self,
        request: CapabilityRequest,
        *,
        workspace: WorkspaceSpec,
        profile: str | None,
        task_policy: CapabilityPolicy | None,
        task_budget: ResourceBudget | None,
        task_deadline: datetime | None,
        runtime_remaining_s: float | None,
        verification_environment: Any,
        directives: DispatchDirectives | None,
        prepared: bool,
    ):
        """Apply task concurrency and resource conflict controls.

        The capability itself remains the authority for effects.  This helper
        only uses the already-declared contract to prevent an unbounded batch
        from exceeding the task budget or racing requests targeting the same
        resource.
        """
        effects: tuple[EffectClass, ...] = ()
        try:
            executor = self._executor_for(request, workspace)
            effects = self._resolve_effects_for(executor.descriptor, request.arguments or {})
        except (CapabilityUnavailable, KeyError, TypeError, ValueError):
            # dispatch() will return the canonical validation/effect error.
            pass

        locks = self._locks_for_request(request, workspace, effects)

        async def invoke_with_locks():
            for lock in locks:
                await lock.acquire()
            try:
                return await self.dispatch(
                    request,
                    workspace=workspace,
                    profile=profile,
                    task_policy=task_policy,
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

        lease = None
        if request.task_id and _is_execution(effects):
            if self._budgets is not None:
                lease = self._budgets.execution_lease(request.task_id)
            elif task_budget is not None:
                # Compatibility for standalone dispatcher users that have not
                # composed a BudgetTracker yet.  Service wiring always binds
                # the hierarchical authority above.
                semaphore = self._execution_semaphores.get(request.task_id)
                if semaphore is None:
                    semaphore = asyncio.Semaphore(max(1, int(task_budget.max_parallel_executions)))
                    self._execution_semaphores[request.task_id] = semaphore
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
            locks.append(self._resource_locks.setdefault(key, asyncio.Lock()))
        return locks

    def _executor_for(self, request: CapabilityRequest, workspace: WorkspaceSpec):
        fabric = self._fabric
        if fabric is not None:
            return fabric.executor_for(
                request.capability_id,
                task_id=request.task_id,
                project_id=workspace.id,
                user_id=self._principal.id,
            )
        return self.registry.executor_for(request.capability_id)

    def resolve_effects(
        self,
        request: CapabilityRequest,
        workspace: WorkspaceSpec,
    ) -> tuple[EffectClass, ...]:
        """Resolve a call's declared effects without invoking it.

        This is used by mediated callers to enforce their own narrower
        authority ceiling before handing the request to ``dispatch``.
        Policy, RealityGate, approvals, and execution remain in ``dispatch``.
        """
        executor = self._executor_for(request, workspace)
        return self._resolve_effects_for(executor.descriptor, request.arguments or {})

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
                executor = self._executor_for(request, workspace)
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
                repaired_args, receipt = self.repairer.repair(
                    call_id=request.call_id,
                    tool_name=request.capability_id,
                    arguments=repair_arguments,
                    input_schema=executor.descriptor.input_schema,
                    validate_fn=validate_schema,
                    mcp_origin=(getattr(executor.descriptor.origin, "value", None) == "MCP"),
                    provider_profile_id=(
                        getattr(candidate, "provider_profile_id", None) or self._provider_profile_id
                    ),
                    model_id=getattr(candidate, "model_id", None) or self._model_id,
                    completion_state=completion_state,
                    mode=self._repair_mode,
                )
                if receipt.outcome == "INVALID":
                    await self._persist_repair(
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
                    await self._persist_repair(
                        request,
                        receipt,
                        original_arguments=repair_arguments,
                        canonical_arguments=repaired_args,
                    )
                    object.__setattr__(request, "arguments", repaired_args)
                    await self._emit_repair(request, receipt, repaired_args)
                else:
                    await self._persist_repair(
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
                self._resolve_effects_for(executor.descriptor, request.arguments or {})
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
        if self._repair_store is None:
            return
        try:
            await self._repair_store.record(
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
        await self._emit(
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
    _EXEC_CAPABILITIES = frozenset(
        {"execute", "terminal_session", "process", "debugger", "shell", "bash"}
    )

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
        return CapabilityDispatcher._resolve_effects(descriptor, arguments)

    @staticmethod
    def _resolve_effects(descriptor, arguments: Mapping[str, Any]) -> tuple[EffectClass, ...]:
        """Resolve the full concrete effect set for bound arguments (BHV-041).

        Multi-effect operations (copy/move, execute) return the combined set so
        the PolicyEngine evaluates every effect, not just one.
        """
        available = descriptor.effects
        op = str(arguments.get("operation") or arguments.get("action") or "").lower()
        want: tuple[EffectClass, ...]

        if descriptor.id in CapabilityDispatcher._EXEC_CAPABILITIES:
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
        if capability_id in task_policy.deny:
            return PolicyVerdict.DENY
        if task_policy.allow and capability_id not in task_policy.allow:
            return PolicyVerdict.DENY
        if (
            task_policy.ask
            and capability_id not in task_policy.ask
            and capability_id not in task_policy.allow
        ):
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

        if self._approval_store is not None:
            persisted = await self._approval_store.create_request(
                task_id=request.task_id,
                capability_id=request.capability_id,
                arguments=dict(arguments),
                approval_id=approval_id,
                metadata=metadata,
            )
            approval_id = persisted

        manager = getattr(self.policy, "approvals", None)
        if manager is not None and manager.state(approval_id) is None:
            primary = _primary_effect(tuple(effects))
            manager.create_request(
                self._principal,
                scope,
                capability=request.capability_id,
                effect=str(primary.value) if primary is not None else None,
                task_id=request.task_id,
                session_id=getattr(request, "session_id", None),
                expires_at=expires_at,
                approval_id=approval_id,
                args_digest=digest,
                call_id=request.call_id,
            )

        if self._continuation_store is not None:
            # Durable continuation (review item 19): the kernel's parked call
            # is in-memory only, so persist enough to reconstruct it after a
            # restart. Failure-isolated — approval parking must not break.
            try:
                await self._continuation_store.record(
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
                        or self._provider_profile_id
                    ),
                    model_id=(getattr(request.candidate, "model_id", None) or self._model_id),
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

        self._resume_expiry[approval_id] = expires_at
        return approval_id

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

    async def _record_mutation(
        self,
        request: CapabilityRequest,
        result: CapabilityResult,
    ) -> None:
        if self._mutation_store is None:
            return
        mutation = (result.metadata or {}).get("mutation")
        if not mutation:
            return
        mutation_id = mutation.get("mutation_id")
        mutation_sequence = None
        if mutation_id and self._mutation_store is not None:
            mutation_sequence = await self._mutation_sequence_for(self._mutation_store, mutation_id)
        if mutation.get("mutation_id"):
            event = await self._emit(
                EV["MUTATION_RECORDED"],
                {
                    "call_id": request.call_id,
                    "capability_id": request.capability_id,
                    "resource": mutation.get("resource"),
                    "operation": mutation.get("operation"),
                    "mutation_id": mutation.get("mutation_id"),
                    "mutation_sequence": mutation_sequence,
                },
                request.task_id,
                causal_id=request.call_id,
            )
            self._attach_mutation_boundary(
                result,
                event_sequence=getattr(event, "sequence", None),
                mutation_sequence=mutation_sequence,
            )
            if self._mutation_observer is not None:
                await self._mutation_observer(
                    request.task_id,
                    mutation.get("resource", ""),
                    mutation.get("mutation_id"),
                    getattr(event, "sequence", None),
                    mutation_sequence,
                )
            return
        try:
            mid = await self._mutation_store.record(
                task_id=request.task_id,
                resource=mutation.get("resource", ""),
                operation=mutation.get("operation", ""),
                before_state=mutation.get("before_hash"),
                after_state=mutation.get("after_hash"),
                reversible=bool(mutation.get("reversible", False)),
                before_ref=mutation.get("before_ref"),
                inverse=mutation.get("inverse"),
                metadata={
                    "capability_call_id": request.call_id,
                    "capability_id": request.capability_id,
                },
            )
        except Exception as e:
            await self._emit(
                "MUTATION_RECORD_FAILED",
                {
                    "call_id": request.call_id,
                    "capability_id": request.capability_id,
                    "resource": mutation.get("resource"),
                    "operation": mutation.get("operation"),
                    "error": str(e),
                },
                request.task_id,
                causal_id=request.call_id,
            )
            import logging

            logging.getLogger("athena.dispatcher").warning(
                "mutation record failed: %s",
                e,
                exc_info=True,
            )
            raise

        mutation_sequence = await self._mutation_sequence_for(self._mutation_store, mid)
        event = await self._emit(
            EV["MUTATION_RECORDED"],
            {
                "call_id": request.call_id,
                "capability_id": request.capability_id,
                "resource": mutation.get("resource"),
                "operation": mutation.get("operation"),
                "mutation_id": mid,
                "mutation_sequence": mutation_sequence,
            },
            request.task_id,
            causal_id=request.call_id,
        )
        self._attach_mutation_boundary(
            result,
            event_sequence=getattr(event, "sequence", None),
            mutation_sequence=mutation_sequence,
        )
        if self._mutation_observer is not None:
            await self._mutation_observer(
                request.task_id,
                mutation.get("resource", ""),
                mid,
                getattr(event, "sequence", None),
                mutation_sequence,
            )

    @staticmethod
    async def _mutation_sequence_for(store, mutation_id: str) -> int | None:
        """Read a sequence when the configured store supports the extension.

        A few embedders provide a compatible pre-sequence MutationStore. They
        must retain the mutation path without losing the newer world-state
        boundary metadata.
        """
        sequence_for = getattr(store, "sequence_for", None)
        if sequence_for is None:
            return None
        return await sequence_for(mutation_id)

    @staticmethod
    def _attach_mutation_boundary(
        result: CapabilityResult,
        *,
        event_sequence: int | None,
        mutation_sequence: int | None,
    ) -> None:
        """Expose the durable mutation boundary to downstream orchestration."""
        metadata = dict(result.metadata or {})
        metadata["mutation_event_sequence"] = event_sequence
        metadata["mutation_sequence"] = mutation_sequence
        object.__setattr__(result, "metadata", metadata)


def _bounded_diagnostics(values: list[Any] | tuple[Any, ...]) -> list[Any]:
    """Keep diagnostic events useful without turning them into an output log."""
    bounded: list[Any] = []
    for value in values[:64]:
        if isinstance(value, Mapping):
            item: dict[str, Any] = {}
            for key, raw in list(value.items())[:24]:
                if isinstance(raw, (str, int, float, bool)) or raw is None:
                    clean = str(raw)[:4096] if isinstance(raw, str) else raw
                    item[str(key)] = clean
            bounded.append(item)
        else:
            bounded.append(str(value)[:4096])
    return bounded


def _is_execution(effects: tuple[EffectClass, ...]) -> bool:
    return bool(
        set(effects)
        & {
            EffectClass.EXECUTE,
            EffectClass.SPAWN_PROCESS,
        }
    )


def _resource_key(workspace: WorkspaceSpec, value: str) -> str:
    """Normalize a capability path for conflict locking."""
    raw = os.path.expanduser(value)
    root = os.path.realpath(os.path.abspath(workspace.root))
    target = os.path.realpath(
        os.path.abspath(raw if os.path.isabs(raw) else os.path.join(root, raw))
    )
    try:
        inside = os.path.commonpath((root, target)) == root
    except ValueError:
        inside = False
    return target if inside else f"external:{target}"


def _cacheable_effects(effects: tuple[EffectClass, ...]) -> bool:
    return bool(effects) and set(effects).issubset({EffectClass.READ_LOCAL})


def _health_state_changed(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
    """Detect circuit transitions, excluding routine closed-counter updates."""
    return str(before.get("status") or "closed") != str(after.get("status") or "closed")


async def _result_cache_key(
    request: CapabilityRequest,
    workspace: WorkspaceSpec,
    effects: tuple[EffectClass, ...],
    *,
    profile: str | None,
    descriptor=None,
) -> tuple[str | None, str, str, str, str | None] | None:
    cache_policy = (
        descriptor.resolve_cache_policy(request.arguments or {})
        if descriptor is not None
        else CachePolicy.NONE
    )
    if descriptor is None or cache_policy is CachePolicy.NONE or not _cacheable_effects(effects):
        return None
    try:
        arguments = json.dumps(
            {
                "workspace_id": workspace.id,
                "session_id": request.session_id,
                "descriptor_version": descriptor.version,
                "arguments": dict(request.arguments or {}),
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except (TypeError, ValueError):
        return None
    if cache_policy is CachePolicy.CONTENT_ADDRESS:
        resolver = descriptor.cache_key_resolver
        if resolver is None:
            return None
        try:
            await asyncio.sleep(0)
            content_key = resolver(request.arguments or {}, workspace)
        except (OSError, TypeError, ValueError):
            return None
        if not content_key:
            return None
        arguments += ":content:" + content_key
    elif cache_policy is CachePolicy.WORKSPACE_REVISION:
        # A caller that owns a persisted workspace revision can put it on the
        # scoped workspace object. Do not synchronously hash a whole repo on
        # the event loop as a fallback; without a revision this policy is off.
        revision = getattr(workspace, "revision", None)
        if not revision:
            return None
        arguments += ":revision:" + str(revision)
    return (
        request.task_id,
        os.path.realpath(os.path.abspath(workspace.root)),
        request.capability_id,
        arguments,
        getattr(profile, "value", profile),
    )


def _cache_ttl(descriptor, cache_policy: CachePolicy | None = None) -> float:
    cache_policy = cache_policy or descriptor.cache_policy
    value = descriptor.cache_ttl_seconds
    if value is None and cache_policy is CachePolicy.TTL:
        return CapabilityDispatcher._RESULT_CACHE_TTL
    if value is None:
        # Revision/content-addressed entries are valid until their key changes
        # or a mutation invalidates the workspace.  Bound the in-memory
        # lifetime so a long-lived service never accumulates stale entries.
        return 3600.0
    return max(0.01, min(float(value), 3600.0))


_SECRET_VALUE = re.compile(
    r"(?i)"
    r"(Bearer\s+\S+)"  # Authorization header value
    r"|(\b(?:sk|pk|rk|ghp|gho|ghu|github_pat)(?:[_\-\s]?)[A-Za-z0-9_\-]{8,}\b)"
    r"|(\bAKIA[0-9A-Z]{16}\b)"
    r"|(\b[a-zA-Z0-9]{40,}\b)"  # long opaque token
)


def _redact(value: str) -> str:
    return _SECRET_VALUE.sub("[REDACTED]", value)


def _redact_event_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Scrub secret-like argument values from an event payload.

    Capability arguments and repair argument snapshots are inspected. Other
    event fields (ids, decision labels, ...) remain readable.
    """
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if key in {"arguments", "original_arguments", "canonical_arguments"}:
            out[key] = _redact_value(value, key=key)
        else:
            out[key] = value
    return out


_SECRET_KEY = re.compile(
    r"(?i)(?:authorization|access[_-]?token|refresh[_-]?token|password|"
    r"secret|api[_-]?key|credential|private[_-]?key|cookie|passphrase)"
)


def _redact_value(value: Any, *, key: str = "") -> Any:
    if _SECRET_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, str):
        return _redact(value)
    if isinstance(value, dict):
        return {k: _redact_value(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(v) for v in value]
    return value


_STRICTNESS = {
    PolicyVerdict.ALLOW.value: 0,
    PolicyVerdict.ASK.value: 1,
    PolicyVerdict.DENY.value: 2,
}


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


def _primary_effect(available) -> EffectClass | None:
    for candidate in (
        EffectClass.WRITE_LOCAL,
        EffectClass.DELETE,
        EffectClass.EXECUTE,
        EffectClass.SPAWN_PROCESS,
        EffectClass.READ_LOCAL,
        EffectClass.PRIVILEGED,
        EffectClass.SECRET_READ,
        EffectClass.FINANCIAL,
    ):
        if candidate in available:
            return candidate
    for eff in available:
        return eff
    return None


def _wrap_exception(exc, request) -> CapabilityResult:
    call_id = getattr(request, "call_id", None) or ""
    return CapabilityResult(
        call_id,
        getattr(request, "capability_id", ""),
        CapabilityResultStatus.FAILED,
        error=f"dispatch failed: {exc}",
    )


__all__ = ["WAITING_APPROVAL", "CapabilityDispatcher", "SuspendedCall"]
