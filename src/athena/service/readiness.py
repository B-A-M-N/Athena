"""Admission/readiness API extracted from the service composition root."""

from __future__ import annotations

from pathlib import Path
import os
from typing import Any

import logging

from athena.protocol.capabilities import EffectClass
from athena.protocol.errors import ServiceNotReady
from athena.protocol.policy import (
    DEFAULT_PRINCIPAL_ID,
    PolicyRequest,
    Principal,
    PolicyVerdict,
)
from athena.protocol.tasks import (
    AgentRequest,
    MutationMode,
    TaskSpec,
    WorkClass,
    capability_id_permitted,
)
from athena.concurrency.autonomy import resolve_task_autonomy
from athena.service.records import default_model_policy
from athena.service.readiness_ports import ReadinessPorts
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from athena.service.service import AthenaService

_logger = logging.getLogger("athena.service.readiness")


class ServiceReadinessAPI:
    """Public admission predicates backed by service-owned readiness facts."""

    def __init__(self, svc: "AthenaService") -> None:
        self._ports = ReadinessPorts(svc)

    async def require_agent_ready(self, request: AgentRequest | None = None) -> None:
        await self._ports.require_provider_ready()
        await self.require_capability_profile_ready()
        if request is None:
            return
        base_policy = request.model_policy or default_model_policy()
        criteria = self._ports.normalize_agent_request_acceptance(request)
        await self._ports.admit_model_roles(
            base_policy,
            self._ports.required_model_roles(base_policy, request.metadata, criteria=criteria),
        )

    async def require_task_ready(self, spec: TaskSpec) -> None:
        await self._ports.require_provider_ready()
        await self.require_capability_profile_ready()
        resources = self._ports.runtime_health().get("resources") or {}
        if int(resources.get("unresolved_count", 0) or 0) > 0:
            raise ServiceNotReady(
                "Task-owned resource cleanup requires recovery before new work is admitted.",
                missing=["resource_teardown"],
            )
        policy = spec.model_policy or default_model_policy()
        await self._ports.admit_model_roles(
            policy,
            self._ports.required_model_roles(
                policy, spec.metadata, criteria=spec.acceptance_criteria
            ),
        )

    async def check_complex_coding_readiness(self, spec: TaskSpec) -> dict[str, Any]:
        """Prove the runtime contract before complex coding may start.

        Candidate isolation, a writable shadow-state/clone backend, a usable
        execution backend, policy permission for canonical proof commands, and
        a derivable independent verification category are required—not
        advisory. Missing project-index evidence remains optional because the
        canonical verifier may derive proof from another inspected source.
        """
        metadata = spec.metadata or {}
        execution_plan = spec.execution_plan
        work_class = getattr(execution_plan, "work_class", None)
        required: list[dict[str, str]] = []
        optional: list[dict[str, str]] = []

        if (
            work_class is not WorkClass.COMPLEX_CODING
            and metadata.get("_athena_work_class") != WorkClass.COMPLEX_CODING.value
        ):
            return {"required": False, "ready": True, "gaps": []}

        ws = spec.workspace
        root = getattr(ws, "root", "") if ws is not None else ""
        if ws is None:
            required.append({"check": "workspace", "detail": "no workspace bound"})
        elif not root:
            required.append({"check": "workspace", "detail": "workspace root is empty"})
        elif not Path(root).is_dir():
            required.append({"check": "workspace", "detail": f"root does not exist: {root}"})
        elif (
            ws.mutation_mode is not MutationMode.SPECULATIVE
            and getattr(execution_plan, "isolation_floor", MutationMode.SPECULATIVE)
            is not MutationMode.SPECULATIVE
        ):
            required.append(
                {"check": "candidate_isolation", "detail": "mutation mode is not speculative"}
            )

        dispatcher = self._ports.dispatcher
        registry = getattr(dispatcher, "registry", None)
        if dispatcher is None or registry is None:
            required.append({"check": "dispatcher", "detail": "dispatcher not started"})
            required.append(
                {"check": "execution_runtime", "detail": "execute capability is unavailable"}
            )
        else:
            try:
                executor = registry.executor_for("execute")
                execute_ready = executor is not None
            except Exception:  # noqa: BLE001 - unavailable executor is the gap
                executor = None
                execute_ready = False
            if not execute_ready:
                required.append(
                    {"check": "execution_runtime", "detail": "execute capability is unavailable"}
                )

        await self._check_candidate_clone_backend(spec, required)
        await self._check_execution_policy(spec, required)
        await self._check_execution_backend(spec, required)
        await self._check_independent_proof(spec, required, optional)

        gaps = required + optional
        return {
            "required": True,
            "ready": not required,
            "gaps": gaps,
            "required_gaps": required,
            "optional_gaps": optional,
        }

    async def _check_candidate_clone_backend(
        self, spec: TaskSpec, required: list[dict[str, str]]
    ) -> None:
        """Prove writable shadow state and the checkpoint clone transport."""
        try:
            shadow = self._ports.shadow_engine()
        except Exception as exc:  # noqa: BLE001 - readiness reports the gap
            required.append({"check": "candidate_workspace", "detail": str(exc)})
            return
        if shadow is None:
            required.append({"check": "candidate_workspace", "detail": "shadow engine unavailable"})
            return
        raw_state_root = getattr(shadow, "_state_root", None)
        if not isinstance(raw_state_root, (str, Path)) or not str(raw_state_root).strip():
            required.append({"check": "candidate_workspace", "detail": "no shadow state root"})
            return
        state_root = Path(raw_state_root)
        raw_roots_parent = getattr(shadow, "_roots_parent", None)
        roots_parent = (
            Path(raw_roots_parent)
            if isinstance(raw_roots_parent, (str, Path)) and str(raw_roots_parent).strip()
            else state_root
        )
        probe = roots_parent
        try:
            probe.mkdir(parents=True, exist_ok=True)
            if not probe.is_dir() or not os.access(probe, os.W_OK | os.X_OK):
                raise OSError(f"shadow state root is not writable: {probe}")
            result = await shadow.preflight_clone_transport()
        except Exception as exc:  # noqa: BLE001 - any probe failure is a readiness gap
            required.append({"check": "candidate_workspace", "detail": str(exc)})
            return
        clone_root = str(result.get("clone_root") or "")
        source_root = str(result.get("source_root") or "")
        if not clone_root or not source_root:
            required.append(
                {"check": "candidate_workspace", "detail": "clone probe did not return proof"}
            )
            return

    async def _check_execution_backend(
        self, spec: TaskSpec, required: list[dict[str, str]]
    ) -> None:
        """Resolve the effective backend before admitting complex work."""
        ws = spec.workspace
        requested = getattr(ws, "execution_backend", None) if ws is not None else None
        execution = self._ports.execution
        if execution is None:
            required.append(
                {"check": "execution_backend", "detail": "execution manager unavailable"}
            )
            return
        try:
            status = await execution.check_backend(requested)
            if not status.get("available", False):
                detail = str(status.get("error") or "execution backend reports unavailable")
                raise RuntimeError(f"execution backend is unavailable: {detail}")
        except Exception as exc:  # noqa: BLE001 - readiness reports the gap
            required.append({"check": "execution_backend", "detail": str(exc)})

    async def _check_execution_policy(self, spec: TaskSpec, required: list[dict[str, str]]) -> None:
        """Use canonical policy algebra for effective execute permission."""
        if not capability_id_permitted("execute", spec.capability_policy):
            required.append(
                {
                    "check": "verification_policy",
                    "detail": "complex coding policy does not permit the execute capability",
                }
            )
            return
        policy = self._ports.policy
        ws = spec.workspace
        if policy is None or ws is None:
            required.append({"check": "verification_policy", "detail": "policy unavailable"})
            return
        request = PolicyRequest(
            principal=Principal("agent", DEFAULT_PRINCIPAL_ID),
            task_id=spec.id,
            capability_id="execute",
            arguments={"operation": "run", "command": "athena-ready-probe"},
            workspace=ws,
            execution_backend=getattr(ws, "execution_backend", None) or "local",
            effects=frozenset({EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS}),
            resources=frozenset(),
            session_id=getattr(spec, "session_id", None),
            call_id=None,
        )
        try:
            autonomy = resolve_task_autonomy(spec.metadata or {})
            decision = policy.evaluate(request, autonomy=autonomy)
        except Exception as exc:  # noqa: BLE001 - readiness reports the gap
            required.append({"check": "verification_policy", "detail": str(exc)})
            return
        if decision.decision is not PolicyVerdict.ALLOW:
            required.append(
                {
                    "check": "verification_policy",
                    "detail": f"effective policy verdict for execute is {decision.decision.value}",
                }
            )

    async def _check_independent_proof(
        self,
        spec: TaskSpec,
        required: list[dict[str, str]],
        optional: list[dict[str, str]],
    ) -> None:
        """Prove that canonical planning can derive at least one category."""
        coordinator = self._ports.reality_coordinator
        verification = getattr(coordinator, "_candidate_verification", None)
        if verification is None:
            required.append(
                {
                    "check": "independent_proof",
                    "detail": "candidate verification service unavailable",
                }
            )
        else:
            try:
                plan = await verification.proof_plan(spec)
            except Exception as exc:  # noqa: BLE001 - readiness reports the gap
                plan = None
                _logger.debug("readiness proof planning failed: %s", exc)
            if plan is None or plan.planning_errors or not plan.criteria:
                required.append(
                    {
                        "check": "independent_proof",
                        "detail": (
                            "; ".join(plan.planning_errors)
                            if plan is not None and plan.planning_errors
                            else "no independent verification criteria could be derived"
                        ),
                    }
                )
        index = self._ports.project_index_coordinator
        if index is None:
            optional.append(
                {
                    "check": "project_index",
                    "detail": "source-verified project index coordinator not available",
                }
            )

    async def require_capability_profile_ready(self) -> None:
        status = await self._ports.validate_required_capabilities()
        if status.get("status") == "ok":
            return
        raise ServiceNotReady(
            "Required capability profile is not ready; new work is admitted only "
            "after every required surface is healthy.",
            profile=status.get("profile"),
            missing=list(status.get("missing") or ()),
        )

    async def refresh_capability_profile(self) -> dict[str, Any]:
        return await self._ports.validate_required_capabilities()


__all__ = ["ServiceReadinessAPI"]
