"""Self-host mission orchestration — extracted from AthenaService (P1-10).

Mechanism, not a second authority. Every decision here routes through the
:class:`AthenaService` composition root it is constructed with: task
admission through the service's trusted seams, planning/completion inference
through ``AgentKernel.utility_inference`` (the one kernel authority),
promotion only through the service-owned mission store and gate bundles.
Relocating this code out of the façade shrinks the façade's mutation
surface; it creates no new authority and changes no decision boundary.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from athena.protocol.ids import new_id
from athena.protocol.tasks import (
    AgentRequest,
    AutonomyLevel,
    ResourceBudget,
    TaskSpec,
    WorkspaceSpec,
)
from athena.self_host.controller import SelfHostMissionController
from athena.self_host.gates import SelfHostGateBundle, SelfHostGatePolicy
from athena.service.self_host_completion import SelfHostCompletionService
from athena.service.self_host_continuation import SelfHostContinuationService
from athena.service.self_host_planning import SelfHostPlanningService
from athena.service.self_host_ports import SelfHostPorts
from athena.service.self_host_verification import preflight_record, verification_environment

if TYPE_CHECKING:
    from athena.service.service import AthenaService

__all__ = ["SelfHostService"]


class SelfHostService:
    """Self-host mission orchestration owned by the AthenaService façade."""

    def __init__(self, service: AthenaService, *, ports: SelfHostPorts | None = None) -> None:
        self._ports = ports or SelfHostPorts(service)
        # Compatibility seam for completion fixtures; validation remains
        # owned by the extracted, service-independent helper.
        self._verification_environment = verification_environment
        self._completion = SelfHostCompletionService(
            self._ports,
            verification_environment=lambda *args, **kwargs: self._verification_environment(
                *args, **kwargs
            ),
        )
        self._planning = SelfHostPlanningService(self._ports)
        self._continuation = SelfHostContinuationService(
            self._ports,
            submit_self_host=self.submit_self_host,
            plan_next=self.plan_next_self_host_item,
            verify_completion=self.verify_self_host_completion,
        )

    def preflight(self, *, workspace_root: str | None = None) -> dict[str, Any]:
        return preflight_record(workspace_root)

    async def submit_self_host(
        self,
        objective: str,
        *,
        workspace_root: str | None = None,
        additional_criteria: tuple[str, ...] = (),
        task_id: str | None = None,
        wait: bool = True,
        mission_id: str | None = None,
        plan: Mapping[str, Any] | None = None,
        _allow_known_dirty: bool = False,
    ) -> TaskSpec:
        """Submit one bounded self-host task through the trusted service path.

        Generic :class:`AgentRequest` metadata cannot opt into this method's
        verification mounts or review boundary.  The CLI is only a caller of
        this service-owned orchestration entrypoint.
        """
        await self._ports.require_agent_ready()
        await self._ports.require_verified_hermes_referee()
        tm = self._ports.require_task_manager()
        root = str(Path(workspace_root or os.getcwd()).resolve())
        planned_task_id = task_id or new_id("task")
        bundle = SelfHostGateBundle.capture(root, allow_dirty=_allow_known_dirty)
        # Bind the task-local context cache and execution provenance to the
        # source revision that the self-host authority actually captured.
        workspace = WorkspaceSpec(
            id="athena-self",
            root=root,
            revision=bundle.source_revision,
        )
        base_fingerprint = await self._ports.shadow_engine().workspace_fingerprint(root)
        if plan is None:
            coordinator = self._ports.project_index_coordinator
            if coordinator is None:
                raise RuntimeError("self-host project index is not started")
            index = await coordinator.current(
                root,
                refresh=True,
                freshness="source_verified",
            )
            plan = SelfHostMissionController.initial_plan(
                str(objective),
                index=index,
                design_bundle_hash=bundle.design_bundle_hash,
                gate_bundle_hash=bundle.gate_bundle_hash,
                base_fingerprint=base_fingerprint,
            )
            budgets = getattr(tm, "budgets", None)
            if task_id is None and budgets is not None:
                from decimal import Decimal

                budgets.register_control_plane(
                    planned_task_id,
                    ResourceBudget(max_cost_usd=Decimal("0.25"), max_parallel_model_calls=1),
                )
            plan, planning_error = await self.plan_next_self_host_item(
                {"objective": str(objective), "plan": plan},
                plan=plan,
                current_index=index,
                bundle=bundle,
                task_id=planned_task_id,
                initial=True,
            )
            if planning_error:
                raise RuntimeError(planning_error)
        else:
            plan = dict(plan)
            evidence = dict(plan.get("evidence") or {})
            evidence.update(
                {
                    "source_revision": bundle.source_revision,
                    "design_bundle_hash": bundle.design_bundle_hash,
                    "gate_bundle_hash": bundle.gate_bundle_hash,
                    "base_fingerprint": base_fingerprint,
                }
            )
            plan["evidence"] = evidence
        plan = SelfHostMissionController.mark_task(plan, planned_task_id)
        planned_prompt = SelfHostMissionController.task_prompt(plan)
        verification = verification_environment(
            workspace,
            include_project_root=True,
            include_rust=True,
            task_id=planned_task_id,
        )
        criteria = SelfHostGatePolicy.required_criteria(
            additional_criteria,
            frozen_safety=bundle.required_commands,
        )
        request = AgentRequest(
            prompt=planned_prompt,
            task_id=planned_task_id,
            workspace=workspace,
            autonomy=AutonomyLevel.CODING,
            metadata={"acceptance_criteria": list(criteria)},
        )
        session_id = request.session_id or new_id("session")
        spec = self._ports.build_task_spec(
            request,
            session_id,
            trusted_verification=verification,
            trusted_self_host=True,
            trusted_gate_criteria=criteria,
            trusted_gate_bundle=bundle.to_record(),
            trusted_mission_plan=plan,
        )
        created = await tm.create(spec)
        budgets = getattr(tm, "budgets", None)
        persist_budget = getattr(budgets, "_persist_usage", None)
        if callable(persist_budget):
            await persist_budget(created.id)
        missions = self._ports.self_host_missions
        if missions is None:
            raise RuntimeError("self-host mission store is not started")
        bundle_record = bundle.to_record()
        if mission_id is None:
            await missions.create(
                project_root=root,
                objective=str(objective),
                task_id=created.id,
                base_revision=str(bundle_record.get("source_revision") or ""),
                design_bundle_hash=str(bundle_record.get("design_bundle_hash") or ""),
                gate_bundle_hash=str(bundle_record.get("gate_bundle_hash") or ""),
                current_base_fingerprint=base_fingerprint,
                current_git_revision=str(bundle_record.get("source_revision") or ""),
                current_design_bundle_hash=str(bundle_record.get("design_bundle_hash") or ""),
                current_gate_bundle_hash=str(bundle_record.get("gate_bundle_hash") or ""),
                plan=plan,
            )
        else:
            await missions.update(
                mission_id,
                status="active",
                current_task_id=created.id,
                last_error=None,
                current_base_fingerprint=base_fingerprint,
                current_git_revision=str(bundle_record.get("source_revision") or ""),
                current_design_bundle_hash=str(bundle_record.get("design_bundle_hash") or ""),
                current_gate_bundle_hash=str(bundle_record.get("gate_bundle_hash") or ""),
                plan=plan,
            )
        # Self-host plans are still user-initiated task turns. Persist their
        # canonical service-owned prompt before the worker can observe them.
        await self._ports.record_canonical_user_turn(request, created)
        await tm.enqueue(created.id)
        if wait:
            await self._ports.wait_for(created.id)
        return created

    async def plan_next_self_host_item(
        self,
        mission: Mapping[str, Any],
        *,
        plan: Mapping[str, Any],
        current_index: Any,
        bundle: SelfHostGateBundle,
        task_id: str | None,
        initial: bool = False,
        current_release_evidence: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        """Compatibility seam for the extracted bounded planner."""
        return await self._planning.plan_next(
            mission,
            plan=plan,
            current_index=current_index,
            bundle=bundle,
            task_id=task_id,
            initial=initial,
            current_release_evidence=current_release_evidence,
        )

    async def verify_self_host_performance(
        self,
        *,
        bundle: SelfHostGateBundle,
        task_id: str | None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        return await self._completion.verify_performance(bundle=bundle, task_id=task_id)

    async def verify_self_host_completion(
        self,
        mission: Mapping[str, Any],
        *,
        plan: Mapping[str, Any],
        current_index: Any,
        bundle: SelfHostGateBundle,
        current_fingerprint: str,
        release_evidence: Mapping[str, Any],
        task_id: str | None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        return await self._completion.verify_completion(
            mission,
            plan=plan,
            current_index=current_index,
            bundle=bundle,
            current_fingerprint=current_fingerprint,
            release_evidence=release_evidence,
            task_id=task_id,
        )

    async def run_hermes_mission_referee(
        self,
        mission: Mapping[str, Any],
        *,
        plan: Mapping[str, Any],
        current_index: Any,
        bundle: SelfHostGateBundle,
        current_fingerprint: str,
        release_evidence: Mapping[str, Any],
        task_id: str | None,
    ) -> dict[str, Any]:
        return await self._completion.run_hermes_referee(
            mission,
            plan=plan,
            current_index=current_index,
            bundle=bundle,
            current_fingerprint=current_fingerprint,
            release_evidence=release_evidence,
            task_id=task_id,
        )

    async def self_host_status(self, *, workspace_root: str | None = None) -> list[dict[str, Any]]:
        missions = self._ports.self_host_missions
        if missions is None:
            raise RuntimeError("self-host mission store is not started")
        root = str(Path(workspace_root or os.getcwd()).resolve())
        records = await missions.list_recent(root)
        for record in records:
            task_id = record.get("current_task_id")
            if task_id:
                record["candidate"] = await self._ports.operator_candidate(str(task_id))
        return records

    async def continue_self_host(
        self,
        *,
        workspace_root: str | None = None,
        mission_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._continuation.continue_mission(
            workspace_root=workspace_root,
            mission_id=mission_id,
        )
