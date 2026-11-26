"""Continuation mechanics for service-owned self-host missions.

This module advances one persisted mission through task, candidate, planning,
and completion states.  It does not own mission persistence, task admission,
or completion authority; those remain explicit operations supplied by
``SelfHostPorts`` and ``SelfHostService``.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from athena.protocol.tasks import FINAL_STATUSES, TaskStatus
from athena.self_host.gates import SelfHostGateBundle

PAUSED_SELF_HOST_STATUSES = frozenset(
    {
        TaskStatus.WAITING_APPROVAL,
        TaskStatus.WAITING_INPUT,
        TaskStatus.BLOCKED,
        TaskStatus.RECOVERY_REQUIRED,
    }
)


class SelfHostContinuationService:
    """Advance one persisted mission through its next bounded state."""

    def __init__(
        self,
        ports: Any,
        *,
        submit_self_host: Callable[..., Awaitable[Any]],
        plan_next: Callable[..., Awaitable[tuple[dict[str, Any], str | None]]],
        verify_completion: Callable[..., Awaitable[tuple[dict[str, Any] | None, str | None]]],
    ) -> None:
        self._ports = ports
        self._submit_self_host = submit_self_host
        self._plan_next = plan_next
        self._verify_completion = verify_completion

    async def continue_mission(
        self,
        *,
        workspace_root: str | None = None,
        mission_id: str | None = None,
    ) -> dict[str, Any]:
        missions = self._ports.self_host_missions
        tm = self._ports.require_task_manager()
        if missions is None:
            raise RuntimeError("self-host mission store is not started")
        root = str(Path(workspace_root or os.getcwd()).resolve())
        mission = (
            await missions.get(mission_id) if mission_id else await missions.latest_active(root)
        )
        if mission is not None and str(mission.get("project_root") or "") != root:
            return {"status": "missing", "error": "mission belongs to another workspace"}
        if mission is None:
            return {"status": "missing", "error": "no active self-host mission"}
        if str(mission.get("status") or "") == "complete":
            proof = (mission.get("plan") or {}).get("completion_proof")
            verification = (
                proof.get("completion_verification") if isinstance(proof, Mapping) else None
            )
            if isinstance(verification, Mapping) and verification.get("complete") is True:
                return {"status": "complete", "mission": mission}
            return {
                "status": "completion_hold",
                "mission": mission,
                "error": "stored completion lacks service-owned verification evidence",
            }
        task_id = str(mission.get("current_task_id") or "")
        candidate = await self._ports.operator_candidate(task_id) if task_id else None
        if candidate is not None:
            if candidate.get("status") == "VERIFIED":
                await missions.update(mission["id"], status="review")
            return {"status": "review", "mission": mission, "candidate": candidate}
        task = (
            await self._ports.store_tasks.get(task_id)
            if self._ports.store_tasks and task_id
            else None
        )
        raw_status = (task or {}).get("status")
        try:
            task_status = TaskStatus(raw_status)
        except (TypeError, ValueError):
            task_status = None
        if task_status in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
            return {"status": "resumed", "mission": mission, "task_id": task_id}
        if task_status is TaskStatus.INTERRUPTED:
            await tm.enqueue(task_id)
            return {"status": "resumed", "mission": mission, "task_id": task_id}
        if task_status in PAUSED_SELF_HOST_STATUSES:
            return {"status": "paused", "mission": mission, "task_id": task_id}
        if task_status in FINAL_STATUSES:
            return await self._continue_final_task(
                missions,
                mission=mission,
                task=task,
                task_id=task_id,
                task_status=task_status,
                root=root,
            )
        return {"status": "pending", "mission": mission, "task_id": task_id}

    async def _continue_final_task(
        self,
        missions: Any,
        *,
        mission: Mapping[str, Any],
        task: Mapping[str, Any] | None,
        task_id: str,
        task_status: TaskStatus,
        root: str,
    ) -> dict[str, Any]:
        expected = str(mission.get("current_base_fingerprint") or "")
        shadow = self._ports.shadow_engine()
        if shadow is None:
            error = "self-host shadow engine is unavailable"
            await missions.update(mission["id"], status="blocked", last_error=error)
            return {"status": "blocked", "mission": mission, "error": error}
        actual = await shadow.workspace_fingerprint(root)
        if expected and actual != expected:
            error = (
                "MISSION STALE: workspace fingerprint changed outside the promoted mission state"
            )
            await missions.update(mission["id"], status="blocked", last_error=error)
            return {"status": "stale", "mission": mission, "error": error}
        plan = dict(mission.get("plan") or {})
        if task_status is TaskStatus.COMPLETE and str(mission.get("status") or "") in {
            "promoted",
            "complete",
        }:
            coordinator = self._ports.project_index_coordinator
            if coordinator is None:
                error = "self-host project index is not started"
                await missions.update(mission["id"], status="blocked", last_error=error)
                return {"status": "blocked", "mission": mission, "error": error}
            try:
                index = await coordinator.current(root, refresh=True, freshness="source_verified")
                bundle = SelfHostGateBundle.capture(root, allow_dirty=True)
                plan, planning_error = await self._plan_next(
                    mission,
                    plan=plan,
                    current_index=index,
                    bundle=bundle,
                    task_id=task_id or None,
                    current_release_evidence=self._release_evidence(
                        mission, task, plan, actual, task_status
                    ),
                )
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                planning_error = f"self-host PLAN NEXT failed: {exc}"
            if planning_error:
                await missions.update(
                    mission["id"], status="blocked", last_error=planning_error, plan=plan
                )
                return {"status": "blocked", "mission": mission, "error": planning_error}
            if plan.get("phase") == "COMPLETION_PROPOSED":
                completion_proof, completion_error = await self._verify_completion(
                    mission,
                    plan=plan,
                    current_index=index,
                    bundle=bundle,
                    current_fingerprint=actual,
                    release_evidence=self._release_evidence(
                        mission, task, plan, actual, task_status
                    ),
                    task_id=task_id or None,
                )
                if completion_error:
                    plan["completion_verification"] = {"status": "hold", "error": completion_error}
                    await missions.update(
                        mission["id"],
                        status="promoted",
                        last_error=completion_error,
                        plan=plan,
                        current_base_fingerprint=actual,
                        current_git_revision=bundle.source_revision,
                        current_design_bundle_hash=bundle.design_bundle_hash,
                        current_gate_bundle_hash=bundle.gate_bundle_hash,
                    )
                    return {
                        "status": "completion_hold",
                        "mission": {**mission, "plan": plan},
                        "error": completion_error,
                    }
                plan["phase"] = "COMPLETE"
                plan["completion_proof"] = completion_proof
                plan["remaining"] = []
                await missions.update(
                    mission["id"],
                    status="complete",
                    last_error=None,
                    plan=plan,
                    current_base_fingerprint=actual,
                    current_git_revision=bundle.source_revision,
                    current_design_bundle_hash=bundle.design_bundle_hash,
                    current_gate_bundle_hash=bundle.gate_bundle_hash,
                )
                return {"status": "complete", "mission": {**mission, "plan": plan}}
            if plan.get("phase") == "COMPLETE":
                proof = plan.get("completion_proof")
                verification = (
                    proof.get("completion_verification") if isinstance(proof, Mapping) else None
                )
                if (
                    not isinstance(verification, Mapping)
                    or verification.get("complete") is not True
                ):
                    error = "stored completion lacks service-owned verification evidence"
                    await missions.update(
                        mission["id"], status="blocked", last_error=error, plan=plan
                    )
                    return {"status": "blocked", "mission": mission, "error": error}
                await missions.update(
                    mission["id"],
                    status="complete",
                    plan=plan,
                    current_base_fingerprint=actual,
                    current_git_revision=bundle.source_revision,
                    current_design_bundle_hash=bundle.design_bundle_hash,
                    current_gate_bundle_hash=bundle.gate_bundle_hash,
                )
                return {"status": "complete", "mission": {**mission, "plan": plan}}
            await missions.update(
                mission["id"],
                status="active",
                plan=plan,
                current_base_fingerprint=actual,
                current_git_revision=bundle.source_revision,
                current_design_bundle_hash=bundle.design_bundle_hash,
                current_gate_bundle_hash=bundle.gate_bundle_hash,
            )
        created = await self._submit_self_host(
            str(
                (plan.get("current_work_item") or {}).get("objective")
                or mission.get("objective")
                or ""
            ),
            workspace_root=root,
            mission_id=str(mission["id"]),
            wait=False,
            plan=plan,
            _allow_known_dirty=True,
        )
        return {"status": "started", "mission": mission, "task_id": created.id}

    @staticmethod
    def _release_evidence(
        mission: Mapping[str, Any],
        task: Mapping[str, Any] | None,
        plan: Mapping[str, Any],
        fingerprint: str,
        task_status: TaskStatus,
    ) -> dict[str, Any]:
        return {
            "task_status": task_status,
            "base_fingerprint": fingerprint,
            "review": plan.get("review"),
            "unresolved_failures": (
                mission.get("last_error") or (task or {}).get("error") or (task or {}).get("reason")
            ),
        }


__all__ = ["PAUSED_SELF_HOST_STATUSES", "SelfHostContinuationService"]
