"""Canonical AgentRequest-to-TaskSpec admission assembly."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from athena.protocol.ids import new_id
from athena.protocol.tasks import (
    AgentRequest,
    AutonomyLevel,
    ContextRef,
    MutationMode,
    NetworkPolicy,
    ResourceBudget,
    SpeculationDepth as ProtocolSpeculationDepth,
    TaskExecutionPlan,
    TaskSpec,
    VerificationStrength,
    WorkClass as ProtocolWorkClass,
)
from athena.execution.environment import VerificationEnvironment
from athena.service.work_classification import SpeculationDepth, WorkClass, decide_speculation
from athena.protocol.task_codec import decode_criteria
from athena.protocol.tasks import CapabilityPolicy
from athena.service.records import default_model_policy
from athena.service.task_intake_ports import TaskIntakePorts

# Privacy values ModelRouter treats as a hard LOCAL-only gate. Network-DENY
# workspaces narrow task model policy into this
# set; "local-preferred" deliberately is not in it because the
OFFLINE_MODEL_PRIVACY = frozenset({"offline", "local"})


__all__ = ["TaskIntake", "TaskIntakePorts"]


_PLAN_CLASS_RANK = {
    ProtocolWorkClass.NON_CODING: 0,
    ProtocolWorkClass.SIMPLE_EDIT: 1,
    ProtocolWorkClass.COMPLEX_CODING: 2,
}

_PLAN_DEPTH_RANK = {
    ProtocolSpeculationDepth.NONE: 0,
    ProtocolSpeculationDepth.SINGLE_CANDIDATE: 1,
    ProtocolSpeculationDepth.MULTI_CANDIDATE: 2,
}

_PLAN_VERIFICATION_RANK = {
    VerificationStrength.NONE: 0,
    VerificationStrength.STANDARD: 1,
    VerificationStrength.STRONG: 2,
}


def _monotonic_plan(persisted: TaskExecutionPlan, derived: TaskExecutionPlan) -> TaskExecutionPlan:
    """Merge a persisted plan and current deterministic floor without downgrade.

    Isolation and proof floors are independently monotonic; the persisted
    work class or speculation depth may be strengthened only when the current
    deterministic classifier proves a higher requirement.
    """
    return TaskExecutionPlan(
        work_class=max(
            (persisted.work_class, derived.work_class),
            key=lambda value: _PLAN_CLASS_RANK[value],
        ),
        speculation_depth=max(
            (persisted.speculation_depth, derived.speculation_depth),
            key=lambda value: _PLAN_DEPTH_RANK[value],
        ),
        isolation_floor=(
            persisted.isolation_floor
            if persisted.isolation_floor is MutationMode.SPECULATIVE
            else derived.isolation_floor
        ),
        verification_floor=max(
            (persisted.verification_floor, derived.verification_floor),
            key=lambda value: _PLAN_VERIFICATION_RANK[value],
        ),
    )


class TaskIntake:
    """Build the canonical TaskSpec at service admission.

    This is a mechanism only: policy and service invariants remain owned by
    AthenaService; TaskIntake receives the configured service and returns one
    immutable TaskSpec.
    """

    @staticmethod
    def normalize_spec(spec: TaskSpec, *, trusted: bool = False) -> TaskSpec:
        """Apply service-owned work classification to an already-built spec.

        Transports may construct provisional protocol objects, but they may
        not decide admission authority. Trusted internal records cannot remove
        a complex-coding decision or forge a stronger one.
        """
        decision = decide_speculation(spec.objective)
        metadata = dict(spec.metadata or {})
        workspace = spec.workspace
        current_plan = TaskExecutionPlan(
            work_class=ProtocolWorkClass(decision.work_class.value),
            speculation_depth=ProtocolSpeculationDepth(decision.depth.value),
            isolation_floor=(
                workspace.mutation_mode if workspace is not None else MutationMode.DIRECT
            ),
            verification_floor=(
                VerificationStrength.STRONG
                if decision.work_class is WorkClass.COMPLEX_CODING
                else VerificationStrength.NONE
            ),
        )
        if trusted and spec.execution_plan is not None:
            # A trusted internal schedule snapshot is admission authority, not
            # a caller suggestion.  Current safety rules may strengthen it,
            # but they may never silently downgrade a persisted decision.
            execution_plan = _monotonic_plan(spec.execution_plan, current_plan)
            metadata["_athena_work_class"] = execution_plan.work_class.value
            metadata["_athena_speculation_depth"] = execution_plan.speculation_depth.value
            if (
                execution_plan.work_class is ProtocolWorkClass.COMPLEX_CODING
                and workspace is not None
                and workspace.mutation_mode is MutationMode.DIRECT
            ):
                workspace = replace(workspace, mutation_mode=MutationMode.SPECULATIVE)
            return replace(
                spec,
                workspace=workspace,
                metadata=metadata,
                execution_plan=execution_plan,
            )
        # Trusted framework metadata may supply protected authority fields,
        # but it never bypasses deterministic safety floors. Apply the floors
        # before preserving any trusted, stricter metadata.
        metadata["_athena_work_class"] = decision.work_class.value
        metadata["_athena_speculation_depth"] = decision.depth.value
        if (
            decision.work_class is WorkClass.COMPLEX_CODING
            and workspace is not None
            and workspace.mutation_mode is MutationMode.DIRECT
        ):
            workspace = replace(workspace, mutation_mode=MutationMode.SPECULATIVE)
        execution_plan = current_plan
        return replace(
            spec,
            workspace=workspace,
            metadata=metadata,
            execution_plan=execution_plan,
        )

    def __init__(self, service: Any, *, ports: TaskIntakePorts | None = None) -> None:
        self._ports = ports or TaskIntakePorts(service)

    def build_spec(
        self,
        request: AgentRequest,
        session_id: str,
        *,
        trusted_verification: VerificationEnvironment | None = None,
        trusted_self_host: bool = False,
        trusted_gate_criteria: tuple[str, ...] = (),
        trusted_gate_bundle: Mapping[str, Any] | None = None,
        trusted_mission_plan: Mapping[str, Any] | None = None,
    ) -> TaskSpec:
        ws = request.workspace or self._ports.default_workspace
        # Omitted autonomy is genuinely omitted: only the service resolves
        # its configured default. Interfaces do not manufacture their own
        # transport-local autonomy defaults.
        autonomy = request.autonomy or self._ports.config.autonomy_level
        self._ports.validate_request_metadata(request.metadata)
        # Preserve request metadata (autonomy + any caller-supplied fields)
        meta: dict[str, Any] = {"autonomy": autonomy.value}
        if request.metadata:
            meta.update(request.metadata)
        self_host = trusted_self_host
        if self_host:
            # Self-hosting is a service invariant, not a CLI convention. A
            # caller cannot escape the candidate/review boundary by sending a
            # direct mutation mode or a permissive network workspace.
            autonomy = AutonomyLevel.CODING
            meta["autonomy"] = autonomy.value
            meta["_athena_self_host"] = True
            meta["_athena_review_before_commit"] = True
            if trusted_verification is None:
                raise ValueError("self-host tasks require a trusted verification environment")
            if not trusted_gate_criteria:
                raise ValueError("self-host tasks require service-owned verification gates")
            meta["_athena_verification_environment"] = trusted_verification.to_record()
            meta["_athena_required_gates"] = list(trusted_gate_criteria)
            # The trusted criteria are the task's actual proof contract.  Do
            # not leave them only in private metadata where the verifier's
            # acceptance evaluator cannot enforce them.
            meta["acceptance_criteria"] = list(trusted_gate_criteria)
            if trusted_gate_bundle is not None:
                meta["_athena_gate_bundle"] = dict(trusted_gate_bundle)
            if trusted_mission_plan is not None:
                meta["_athena_mission_plan"] = dict(trusted_mission_plan)
            ws = replace(
                ws,
                network_policy=NetworkPolicy.DENY,
                mutation_mode=MutationMode.SPECULATIVE,
            )
        legacy_mutation_mode = meta.pop("mutation_mode", None)
        typed_mutation_mode = request.mutation_mode
        if typed_mutation_mode is not None and legacy_mutation_mode is not None:
            try:
                legacy_value = MutationMode(str(legacy_mutation_mode))
            except ValueError as exc:
                raise ValueError("legacy mutation_mode conflicts with typed mutation_mode") from exc
            if legacy_value is not typed_mutation_mode:
                raise ValueError("typed mutation_mode conflicts with legacy metadata mutation_mode")
        raw_mutation_mode = (
            typed_mutation_mode if typed_mutation_mode is not None else legacy_mutation_mode
        )
        work_class = WorkClass.of(request.prompt)
        # OFFLINE autonomy is a hard egress boundary (P0): the task's model
        # routing is narrowed to local-only models, not merely biased toward
        # them. Model calls do not pass through PolicyEngine; ModelRouter's
        # privacy gate is the authority that owns this boundary, and it only
        # enforces what the task's ModelPolicy carries. A network-DENY
        # workspace pins model egress the same way.
        model_policy = request.model_policy or default_model_policy()
        if autonomy is AutonomyLevel.OFFLINE or (
            ws.network_policy is not None
            and ws.network_policy == NetworkPolicy.DENY
            and model_policy.privacy not in OFFLINE_MODEL_PRIVACY
        ):
            if model_policy.privacy not in OFFLINE_MODEL_PRIVACY:
                model_policy = replace(model_policy, privacy="offline")
                meta["_athena_offline_narrowed"] = True
        if self_host:
            # The service-enforced self-host boundary wins over any copied
            # request metadata, including an explicit direct-mode escape.
            ws = replace(ws, mutation_mode=MutationMode.SPECULATIVE)
        elif raw_mutation_mode is not None:
            try:
                mutation_mode = (
                    raw_mutation_mode
                    if isinstance(raw_mutation_mode, MutationMode)
                    else MutationMode(str(raw_mutation_mode))
                )
            except ValueError as exc:
                raise ValueError(
                    "mutation_mode must be one of: "
                    + ", ".join(mode.value for mode in MutationMode)
                ) from exc
            ws = replace(ws, mutation_mode=mutation_mode)
        elif autonomy is AutonomyLevel.CODING:
            # Coding tasks are protected by default.  The escape hatch is
            # explicit metadata (mutation_mode=direct), never a model choice.
            ws = replace(ws, mutation_mode=MutationMode.SPECULATIVE)
        elif work_class is WorkClass.COMPLEX_CODING:
            # Deterministic admission authority: complex coding work gets a
            # task-local candidate before its first mutation. This is not
            # model guidance and is not overridable by the model.
            ws = replace(ws, mutation_mode=MutationMode.SPECULATIVE)
        protocol_work_class = ProtocolWorkClass(work_class.value)
        protocol_depth = (
            ProtocolSpeculationDepth.SINGLE_CANDIDATE
            if work_class is WorkClass.COMPLEX_CODING
            else ProtocolSpeculationDepth.NONE
        )
        verification_floor = (
            VerificationStrength.STRONG
            if work_class is WorkClass.COMPLEX_CODING
            else VerificationStrength.NONE
        )
        execution_plan = TaskExecutionPlan(
            work_class=protocol_work_class,
            speculation_depth=protocol_depth,
            isolation_floor=ws.mutation_mode if ws is not None else MutationMode.DIRECT,
            verification_floor=verification_floor,
        )
        if work_class is not WorkClass.NON_CODING:
            meta["_athena_work_class"] = work_class.value
            meta["_athena_speculation_depth"] = (
                SpeculationDepth.SINGLE_CANDIDATE.value
                if work_class is WorkClass.COMPLEX_CODING
                else SpeculationDepth.NONE.value
            )
        # Typed acceptance criteria are authoritative. The legacy metadata
        # spelling remains a compatibility input, but a request carrying both
        # forms must agree canonically instead of silently choosing one.
        raw_criteria = meta.pop("acceptance_criteria", None)
        legacy_criteria = decode_criteria(raw_criteria) if raw_criteria is not None else ()
        if self_host:
            criteria = legacy_criteria
        else:
            criteria = self._ports.normalize_acceptance(request)
        # Normalize requested_capabilities into the task's capability policy
        cap_policy = request.capability_policy
        if request.requested_capabilities:
            requested = tuple(sorted(request.requested_capabilities))
            if cap_policy is not None and set(requested) != set(cap_policy.allow):
                raise ValueError("requested_capabilities conflicts with capability_policy.allow")
            cap_policy = cap_policy or CapabilityPolicy(allow=requested)
        # Persist attachments as context refs so they survive beyond the request
        context_refs: list[ContextRef] = []
        for att in request.attachments or []:
            if isinstance(att, ContextRef):
                context_refs.append(att)
            elif hasattr(att, "uri") and hasattr(att, "mime_type"):
                # ArtifactRef attachments need to survive the request boundary
                # as canonical context refs, including their media type.
                context_refs.append(
                    ContextRef(
                        kind="artifact",
                        ref=str(att.uri),
                        source_id=getattr(att, "id", None),
                        summary=getattr(att, "producer", None),
                        mime_type=getattr(att, "mime_type", None),
                    )
                )
            elif isinstance(att, dict):
                context_refs.append(
                    ContextRef(
                        kind=att.get("kind", "artifact"),
                        ref=str(att.get("ref", att.get("uri", "")) or ""),
                        source_id=att.get("source_id"),
                        summary=att.get("summary"),
                        mime_type=att.get("mime_type"),
                    )
                )
        spec_kwargs: dict = dict(
            id=request.task_id or new_id("task"),
            objective=request.prompt,
            session_id=session_id,
            workspace=ws,
            model_policy=model_policy,
            resource_budget=request.resource_budget or ResourceBudget(),
            deadline=request.deadline,
            context_refs=tuple(context_refs),
            metadata=meta,
            execution_plan=execution_plan,
        )
        if criteria:
            spec_kwargs["acceptance_criteria"] = tuple(criteria)
        if cap_policy is not None:
            spec_kwargs["capability_policy"] = cap_policy
        return TaskSpec(**spec_kwargs)
