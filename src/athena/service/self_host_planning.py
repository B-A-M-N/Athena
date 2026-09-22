"""Bounded self-host mission planning through the canonical kernel."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from athena.self_host.controller import SelfHostMissionController
from athena.self_host.gates import SelfHostGateBundle


class SelfHostPlanningService:
    """Prepare one next mission item without owning mission persistence."""

    def __init__(self, ports: Any) -> None:
        self._ports = ports

    async def plan_next(
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
        """Ask the existing kernel for one bounded next mission item."""
        controller = SelfHostMissionController
        kernel = self._ports.kernel
        updated_plan: dict[str, Any] = json.loads(json.dumps(dict(plan), sort_keys=True))
        plan = updated_plan
        evidence = dict(plan.get("evidence") or {})
        evidence.update(
            {
                "source_revision": bundle.source_revision,
                "design_bundle_hash": bundle.design_bundle_hash,
                "gate_bundle_hash": bundle.gate_bundle_hash,
            }
        )
        plan["evidence"] = evidence
        completed_items = plan.get("completed_work_items") or ()
        context_paths = [
            str(path)
            for item in completed_items[-3:]
            if isinstance(item, Mapping)
            for path in item.get("affected_files") or ()
        ]
        context_invariants = [
            str(invariant)
            for item in completed_items[-3:]
            if isinstance(item, Mapping)
            for invariant in item.get("affected_invariants") or ()
        ]
        design_context = bundle.retrieve_design_context(
            paths=context_paths,
            invariants=context_invariants,
        )
        mission_for_prompt = dict(mission)
        mission_for_prompt["plan"] = plan
        prompt = controller.planner_prompt(
            mission_for_prompt,
            current_index=current_index,
            design_context=design_context,
            current_release_evidence=current_release_evidence,
        )
        response = None
        if kernel is not None:
            response = await kernel.utility_inference(
                system_prompt=(
                    "You are Athena's bounded planning role. Use only the trusted "
                    "source index and frozen design context supplied by the service. "
                    "Return the requested JSON shape. Never claim promotion authority."
                ),
                user_prompt=prompt,
                role="planner",
                task_id=task_id,
                metadata={"purpose": "self_host_plan"},
            )
        indexed_files = {
            str(value.get("path"))
            for value in (getattr(current_index, "files", ()) or ())
            if isinstance(value, Mapping) and value.get("path")
        }
        item, reason, error = controller.parse_planner_output(
            response,
            indexed_files=indexed_files,
        )
        if item is not None:
            return controller.replace_current_item(
                plan, item, reason=reason or "planner item"
            ), None
        completed = plan.get("completed_work_items") or ()
        if initial:
            fallback = dict(plan.get("current_work_item") or {})
            fallback["affected_invariants"] = [
                "candidate-isolated",
                "proof-before-promotion",
            ]
            return (
                controller.replace_current_item(
                    plan,
                    fallback,
                    reason=error or "planner unavailable; using bounded objective seed",
                ),
                None,
            )
        if reason and completed:
            return controller.propose_completion(plan, reason=reason), None
        return dict(plan), error or "planner did not produce a next work item"


__all__ = ["SelfHostPlanningService"]
