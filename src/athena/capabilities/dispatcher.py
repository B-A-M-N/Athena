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
import re
from datetime import datetime, timedelta
from typing import Any, Mapping

from athena.capabilities.registry import CapabilityRegistry, validate_schema
from athena.policy.approvals import args_digest
from athena.policy.engine import PolicyEngine
from athena.protocol.capabilities import (
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
    InvocationContext,
)
from athena.protocol.events import EV, make_event
from athena.protocol.ids import new_id
from athena.protocol.policy import (
    ApprovalScope,
    PolicyDecision,
    PolicyRequest,
    PolicyVerdict,
    Principal,
)
from athena.protocol.tasks import CapabilityPolicy, WorkspaceSpec
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
    ) -> None:
        self.call_id = call_id
        self.request = request
        self.decision = decision
        self.approval_id = approval_id


WAITING_APPROVAL = "WAITING_APPROVAL"


class CapabilityDispatcher:
    """Owns the capability invocation lifecycle and the single policy path."""

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
        event_sink=None,
    ) -> None:
        self.registry = registry
        self.policy = policy_engine
        self._principal = principal or Principal("agent", "athena")
        # Inference Compatibility Kernel: deterministic tool-input repair.
        from athena.models.compat.toolrepair import ToolInputRepairer
        from athena.models.compat.profiles import CompatibilityCandidates

        self.repairer = repairer or ToolInputRepairer(
            mode="safe", candidates=candidates or CompatibilityCandidates())
        self._mutation_store = mutation_store
        self._artifact_store = artifact_store
        self._approval_store = approval_store
        self._event_sink = event_sink
        self._suspended: dict[str, SuspendedCall] = {}
        self._resume_expiry: dict[str, datetime] = {}

    # ------------------------------------------------------------------ #
    # Event emission
    # ------------------------------------------------------------------ #
    async def _emit(self, type_: str, payload: Mapping[str, Any], task_id: str | None,
                    causal_id: str | None = None) -> None:
        if self._event_sink is None:
            return
        payload = _redact_event_payload(dict(payload))
        event = make_event(type_, payload, task_id=task_id, causal_id=causal_id)
        await self._event_sink(event)

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
    ) -> CapabilityResult | SuspendedCall:
        """Execute one capability call through the full lifecycle.

        ``task_policy`` (the calling task's CapabilityPolicy) is enforced as a
        HARD ceiling BEFORE the global/profile policy: denote it a hard deny, and
        global policy can only narrow further, never expand task authority.
        """
        if not request.call_id:
            object.__setattr__(request, "call_id", new_id("call"))

        await self._emit(EV["CAPABILITY_REQUESTED"], {
            "call_id": request.call_id,
            "capability_id": request.capability_id,
            "arguments": dict(request.arguments or {}),
        }, request.task_id, causal_id=request.call_id)

        executor = self.registry.executor_for(request.capability_id)
        self._inject_stores(executor)

        # Inference Compatibility Kernel: deterministic repair BEFORE policy.
        # Model-produced calls get one bounded schema-directed repair pass;
        # strict revalidation follows; policy/approval/execution all see the
        # exact canonical arguments. (Spec invariant: repair changes syntax
        # and representation only — never capability or trust.)
        repaired_args, receipt = self.repairer.repair(
            call_id=request.call_id,
            tool_name=request.capability_id,
            arguments=dict(request.arguments or {}),
            input_schema=executor.descriptor.input_schema,
            validate_fn=validate_schema,
            provider_profile_id=getattr(self, "_provider_profile_id", None),
            model_id=getattr(self, "_model_id", None),
        )
        if receipt.outcome == "INVALID":
            result = CapabilityResult(
                request.call_id, request.capability_id,
                CapabilityResultStatus.FAILED,
                error="tool_input_invalid: "
                      + "; ".join(receipt.issue_codes) or "invalid arguments",
                metadata={"repair": receipt.to_dict()},
            )
            await self._emit(EV["CAPABILITY_FAILED"], {
                "call_id": request.call_id,
                "capability_id": request.capability_id,
                "reason": "tool_input_invalid",
                "error": result.error,
            }, request.task_id, causal_id=request.call_id)
            return result
        if receipt.outcome == "REPAIRED":
            object.__setattr__(request, "arguments", repaired_args)
            await self._emit("ToolRepaired", {
                "call_id": request.call_id,
                "tool_name": request.capability_id,
                "rules": receipt.rules,
                "policy_version": receipt.repair_policy_version,
                "schema_hash": receipt.schema_hash,
            }, request.task_id, causal_id=request.call_id)

        errors = validate_schema(executor.descriptor.input_schema, request.arguments or {})
        if errors:
            result = CapabilityResult(
                request.call_id, request.capability_id,
                CapabilityResultStatus.FAILED,
                error="validation failed: " + "; ".join(errors),
            )
            await self._emit(EV["CAPABILITY_FAILED"], {
                "call_id": request.call_id, "capability_id": request.capability_id,
                "reason": "schema_validation", "error": result.error,
            }, request.task_id, causal_id=request.call_id)
            return result

        await self._emit(EV["CAPABILITY_VALIDATED"], {
            "call_id": request.call_id, "capability_id": request.capability_id,
        }, request.task_id, causal_id=request.call_id)

        effects = self._resolve_effects(executor.descriptor, request.arguments or {})
        policy_request = PolicyRequest(
            principal=self._principal,
            task_id=request.task_id,
            capability_id=request.capability_id,
            arguments=dict(request.arguments or {}),
            workspace=workspace,
            execution_backend=workspace.execution_backend,
            effects=frozenset(effects),
            session_id=getattr(request, "session_id", None),
            call_id=request.call_id,
        )
        decision = self.policy.evaluate(policy_request, autonomy=profile)
        global_verdict = _verdict(decision.decision)

        task_verdict = self._eval_task_policy(request.capability_id, task_policy, request_effects=frozenset(effects))
        combined, reason = _combine_verdicts(
            task_verdict, global_verdict, decision.reason
        )
        await self._emit(EV["POLICY_DECISION_MADE"], {
            "call_id": request.call_id,
            "capability_id": request.capability_id,
            "decision": combined.value,
            "reason": reason,
            "matched_rule": decision.matched_rule,
        }, request.task_id, causal_id=request.call_id)

        if combined == PolicyVerdict.DENY:
            if global_verdict == PolicyVerdict.DENY:
                error = f"denied: {decision.reason}"
            else:
                error = f"denied: task capability policy forbids {request.capability_id}"
            await self._emit(EV["CAPABILITY_FAILED"], {
                "call_id": request.call_id, "reason": "denied",
                "matched_rule": decision.matched_rule,
            }, request.task_id, causal_id=request.call_id)
            return CapabilityResult(
                request.call_id, request.capability_id,
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
            )
            suspended = SuspendedCall(request.call_id, request, decision, approval_id)
            self._suspended[request.call_id] = suspended
            await self._emit(EV["APPROVAL_REQUESTED"], {
                "call_id": request.call_id, "capability_id": request.capability_id,
                "task_id": request.task_id,
                "approval_id": approval_id,
                "scopes": [s.value for s in decision.approval_scope_options],
            }, request.task_id, causal_id=request.call_id)
            return suspended

        await self._emit(EV["CAPABILITY_STARTED"], {
            "call_id": request.call_id, "capability_id": request.capability_id,
        }, request.task_id, causal_id=request.call_id)

        context = InvocationContext(
            task_id=request.task_id,
            workspace=workspace,
            execution_backend=workspace.execution_backend,
        )
        result = await executor.invoke(request, context=context)
        if result.status == CapabilityResultStatus.OK:
            await self._record_mutation(request, result)
            await self._emit(EV["CAPABILITY_COMPLETED"], {
                "call_id": request.call_id, "capability_id": request.capability_id,
            }, request.task_id, causal_id=request.call_id)
        else:
            await self._emit(EV["CAPABILITY_FAILED"], {
                "call_id": request.call_id, "capability_id": request.capability_id,
                "reason": result.error,
            }, request.task_id, causal_id=request.call_id)
        return result

    async def dispatch_many(
        self,
        requests: list[CapabilityRequest],
        *,
        workspace: WorkspaceSpec,
        profile: str | None = None,
        task_policy: CapabilityPolicy | None = None,
    ) -> list[CapabilityResult | SuspendedCall]:
        """Dispatch multiple independent capability calls in parallel (BHV-041)."""
        if not requests:
            return []
        results = await asyncio.gather(
            *[self.dispatch(r, workspace=workspace, profile=profile,
                            task_policy=task_policy) for r in requests],
            return_exceptions=True,
        )
        return [_wrap_exception(r, requests[i]) if isinstance(r, BaseException) else r
                for i, r in enumerate(results)]

    # ------------------------------------------------------------------ #
    # Resolution / mutation
    # ------------------------------------------------------------------ #
    # Capabilities whose operations are process/code operations, not file
    # writes — even when an op name like "create" would suggest a write.
    _EXEC_CAPABILITIES = frozenset({"execute", "terminal_session", "process",
                                    "debugger", "shell", "bash"})

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
        if (task_policy.ask
                and capability_id not in task_policy.ask
                and capability_id not in task_policy.allow):
            return PolicyVerdict.DENY
        if capability_id in task_policy.ask:
            return PolicyVerdict.ASK
        # Enforce effect ceiling: task effects must cover request effects
        if request_effects and task_policy.effects:
            if not request_effects.issubset(task_policy.effects):
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
        expires_at = datetime.now() + timedelta(hours=24)
        approval_id = new_id("apr")
        metadata = {
            "call_id": request.call_id,
            "args_digest": digest,
            "capability_id": request.capability_id,
            "effects": [e.value for e in effects],
            "workspace": workspace.id,
            "execution_backend": workspace.execution_backend,
            "scope": scope.value,
            "requested_scope": [s.value for s in decision.approval_scope_options],
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
        if manager is not None:
            if manager.state(approval_id) is None:
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
            except Exception:
                pass
        if self._artifact_store is not None and not getattr(executor, "artifact_store", None):
            try:
                executor.artifact_store = self._artifact_store
            except Exception:
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
        if mutation.get("mutation_id"):
            await self._emit(EV["MUTATION_RECORDED"], {
                "call_id": request.call_id,
                "capability_id": request.capability_id,
                "resource": mutation.get("resource"),
                "operation": mutation.get("operation"),
                "mutation_id": mutation.get("mutation_id"),
            }, request.task_id, causal_id=request.call_id)
            return
        try:
            await self._mutation_store.record(
                task_id=request.task_id,
                resource=mutation.get("resource", ""),
                operation=mutation.get("operation", ""),
                before_state=mutation.get("before_hash"),
                after_state=mutation.get("after_hash"),
                reversible=bool(mutation.get("reversible", False)),
                before_ref=mutation.get("before_ref"),
                inverse=mutation.get("inverse"),
                metadata={"capability_call_id": request.call_id,
                          "capability_id": request.capability_id},
            )
        except Exception as e:
            await self._emit("MUTATION_RECORD_FAILED", {
                "call_id": request.call_id,
                "capability_id": request.capability_id,
                "resource": mutation.get("resource"),
                "operation": mutation.get("operation"),
                "error": str(e),
            }, request.task_id, causal_id=request.call_id)
            import logging
            logging.getLogger("athena.dispatcher").warning(
                "mutation record failed: %s", e, exc_info=True,
            )
            raise

        await self._emit(EV["MUTATION_RECORDED"], {
            "call_id": request.call_id,
            "capability_id": request.capability_id,
            "resource": mutation.get("resource"),
            "operation": mutation.get("operation"),
        }, request.task_id, causal_id=request.call_id)


_SECRET_VALUE = re.compile(
    r"(?i)"
    r"(Bearer\s+\S+)"                      # Authorization header value
    r"|(\b(?:sk|pk|rk|ghp|gho|ghu|github_pat)(?:[_\-\s]?)[A-Za-z0-9_\-]{8,}\b)"
    r"|(\bAKIA[0-9A-Z]{16}\b)"
    r"|(\b[a-zA-Z0-9]{40,}\b)"             # long opaque token
)


def _redact(value: str) -> str:
    return _SECRET_VALUE.sub("[REDACTED]", value)


def _redact_event_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Scrub secret-like argument values from an event payload.

    Only capability ``arguments`` are inspected (contacts the real secrets a
    model might route through a capability). Non-argument event fields
    (ids, decision labels, ...) are left untouched so the audit log stays
    readable. Execution itself is unaffected — this is presentation-only.
    """
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if key == "arguments":
            out[key] = _redact_value(value)
        else:
            out[key] = value
    return out


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact(value)
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
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
        EffectClass.WRITE_LOCAL, EffectClass.DELETE, EffectClass.EXECUTE,
        EffectClass.SPAWN_PROCESS, EffectClass.READ_LOCAL, EffectClass.PRIVILEGED,
        EffectClass.SECRET_READ, EffectClass.FINANCIAL,
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
        getattr(request, "capability_id", "") ,
        CapabilityResultStatus.FAILED,
        error=f"dispatch failed: {exc}",
    )


__all__ = ["CapabilityDispatcher", "SuspendedCall", "WAITING_APPROVAL"]