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

import inspect
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from athena.hermes import HermesDecision, HermesVerdict, ReviewPacket
from athena.protocol.ids import new_id
from athena.protocol.tasks import (
    AgentRequest,
    AutonomyLevel,
    MutationMode,
    NetworkPolicy,
    ResourceBudget,
    TaskSpec,
    TaskStatus,
    WorkspaceSpec,
)
from athena.self_host.controller import SelfHostMissionController
from athena.service._parsing import parse_self_host_completion_verdict
from athena.self_host.gates import SelfHostGateBundle, SelfHostGatePolicy
from athena.service.config import HermesSupervisionMode

if TYPE_CHECKING:
    from athena.service.service import AthenaService

__all__ = ["SelfHostService"]


class SelfHostService:
    """Self-host mission orchestration owned by the AthenaService façade."""

    def __init__(self, service: AthenaService) -> None:
        self._svc = service
        self._fault_injector: Any = None

    def set_fault_injector(self, injector: Any = None) -> None:
        """Install a narrow test hook for self-host intake crash windows."""
        self._fault_injector = injector

    async def _fault_point(self, name: str) -> None:
        if self._fault_injector is None:
            return
        result = self._fault_injector(name)
        if inspect.isawaitable(result):
            await result

    async def reconcile_created_task(self, task: TaskSpec) -> bool:
        """Ensure a CREATED self-host task has a matching mission anchor.

        This runs before ordinary intake recovery. A self-host task is never
        allowed to become runnable merely because its generic canonical-turn
        protocol can be completed; its mission identity is part of the safety
        boundary.
        """
        metadata = dict(task.metadata or {})
        mission_id = str(metadata.get("_self_host_mission_id") or "")
        raw_record = metadata.get("_self_host_intake")
        record = dict(raw_record) if isinstance(raw_record, Mapping) else {}
        missions = getattr(self._svc, "_self_host_missions", None)
        manager = self._svc._require_task_manager()

        async def quarantine(reason: str) -> bool:
            await manager.transition(task.id, TaskStatus.RECOVERY_REQUIRED, reason=reason)
            return False

        if not mission_id or record.get("mission_id") != mission_id:
            return await quarantine("self-host CREATED task has no trusted mission identity")
        if missions is None:
            return await quarantine("self-host mission store is unavailable during recovery")

        mission = await missions.get(mission_id)
        if mission is None:
            required = (
                "project_root",
                "objective",
                "task_id",
                "base_revision",
                "design_bundle_hash",
                "gate_bundle_hash",
                "plan",
            )
            if (
                any(not record.get(key) for key in required)
                or str(record.get("task_id")) != str(task.id)
                or not isinstance(record.get("plan"), Mapping)
            ):
                return await quarantine(
                    "self-host mission is missing and trusted reconstruction metadata is incomplete"
                )
            try:
                mission = await missions.create(
                    mission_id=mission_id,
                    project_root=str(record["project_root"]),
                    objective=str(record["objective"]),
                    task_id=str(record["task_id"]),
                    base_revision=str(record["base_revision"]),
                    design_bundle_hash=str(record["design_bundle_hash"]),
                    gate_bundle_hash=str(record["gate_bundle_hash"]),
                    current_base_fingerprint=record.get("current_base_fingerprint"),
                    current_git_revision=record.get("current_git_revision"),
                    current_design_bundle_hash=record.get("current_design_bundle_hash"),
                    current_gate_bundle_hash=record.get("current_gate_bundle_hash"),
                    plan=record["plan"],
                )
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                return await quarantine(f"self-host mission reconstruction failed: {exc}")

        identity = (
            ("project_root", str(record.get("project_root") or "")),
            ("objective", str(record.get("objective") or "")),
            ("base_revision", str(record.get("base_revision") or "")),
            ("design_bundle_hash", str(record.get("design_bundle_hash") or "")),
            ("gate_bundle_hash", str(record.get("gate_bundle_hash") or "")),
        )
        if any(mission.get(key) != value for key, value in identity):
            return await quarantine("self-host task and mission identity do not match")
        current_task_id = str(mission.get("current_task_id") or "")
        if current_task_id and current_task_id != str(task.id):
            return await quarantine("self-host mission points at a different task")
        if not current_task_id:
            await missions.update(mission_id, current_task_id=str(task.id))
        return True

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
        await self._svc.require_agent_ready()
        await self._svc._require_verified_hermes_referee()
        tm = self._svc._require_task_manager()
        root = str(Path(workspace_root or os.getcwd()).resolve())
        planned_task_id = task_id or new_id("task")
        # Allocate the mission identity before its Task row so recovery never
        # has to infer which mission owns a CREATED self-host task.
        mission_id = mission_id or new_id("mission")
        bundle = SelfHostGateBundle.capture(root, allow_dirty=_allow_known_dirty)
        # Bind the task-local context cache and execution provenance to the
        # source revision that the self-host authority actually captured.
        workspace = WorkspaceSpec(
            id="athena-self",
            root=root,
            revision=bundle.source_revision,
        )
        base_fingerprint = await self._svc.shadow_engine().workspace_fingerprint(root)
        if plan is None:
            coordinator = self._svc._project_index_coordinator
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
            plan, planning_error = await self._plan_next_self_host_item(
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
        verification = self._svc._self_host_verification_environment(
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
        spec = self._svc._build_task_spec(
            request,
            session_id,
            trusted_verification=verification,
            trusted_self_host=True,
            trusted_gate_criteria=criteria,
            trusted_gate_bundle=bundle.to_record(),
            trusted_mission_plan=plan,
        )
        missions = self._svc._self_host_missions
        if missions is None:
            raise RuntimeError("self-host mission store is not started")
        bundle_record = bundle.to_record()
        mission_identity = {
            "mission_id": mission_id,
            "project_root": root,
            "objective": str(objective),
            "task_id": planned_task_id,
            "base_revision": str(bundle_record.get("source_revision") or ""),
            "design_bundle_hash": str(bundle_record.get("design_bundle_hash") or ""),
            "gate_bundle_hash": str(bundle_record.get("gate_bundle_hash") or ""),
            "current_base_fingerprint": base_fingerprint,
            "current_git_revision": str(bundle_record.get("source_revision") or ""),
            "current_design_bundle_hash": str(bundle_record.get("design_bundle_hash") or ""),
            "current_gate_bundle_hash": str(bundle_record.get("gate_bundle_hash") or ""),
            "plan": plan,
        }
        spec = replace(
            spec,
            metadata={
                **dict(spec.metadata),
                "_intake_owner": "self_host",
                "_self_host_mission_id": mission_id,
                # Service-created metadata used only for trusted intake
                # recovery if an older database has no mission row.
                "_self_host_intake": mission_identity,
                "_intake_phase": "task_created",
            },
        )
        # Persist the recovery anchor before creating the Task row. Explicit
        # IDs make retries safe and prevent a missionless CREATED task.
        existing_mission = await missions.get(mission_id)
        if existing_mission is None:
            await missions.create(**mission_identity)
        elif existing_mission.get("project_root") != root or existing_mission.get(
            "objective"
        ) != str(objective):
            raise ValueError(f"self-host mission id {mission_id!r} identifies different work")
        else:
            await missions.update(
                mission_id,
                status="active",
                current_task_id=planned_task_id,
                last_error=None,
                current_base_fingerprint=base_fingerprint,
                current_git_revision=str(bundle_record.get("source_revision") or ""),
                current_design_bundle_hash=str(bundle_record.get("design_bundle_hash") or ""),
                current_gate_bundle_hash=str(bundle_record.get("gate_bundle_hash") or ""),
                plan=plan,
            )
        await self._fault_point("self-host-mission-persisted")
        created = await tm.create(spec)
        await self._fault_point("self-host-task-created")
        budgets = getattr(tm, "budgets", None)
        persist_budget = getattr(budgets, "_persist_usage", None)
        if callable(persist_budget):
            await persist_budget(created.id)
        await self._mark_intake_phase(created.id, "task_created")
        # Self-host plans are still user-initiated task turns. Persist their
        # canonical service-owned prompt before the worker can observe them.
        await self._svc._record_canonical_user_turn(request, created)
        await self._fault_point("self-host-canonical-user-turn-persisted")
        await self._mark_intake_phase(created.id, "canonical_user_turn_persisted")
        await tm.enqueue(created.id)
        await self._fault_point("self-host-enqueued")
        await self._mark_intake_phase(created.id, "enqueued")
        if wait:
            await self._svc.wait_for(created.id)
        return created

    async def _mark_intake_phase(self, task_id: str, phase: str) -> None:
        store = getattr(self._svc, "_store_tasks", None)
        update = getattr(store, "update_metadata", None)
        if callable(update):
            await update(str(task_id), {"_intake_phase": phase})

    async def _plan_next_self_host_item(
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
        kernel = self._svc._kernel
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
            # A source checkout without a configured planner still gets one
            # bounded ordinary coding task; continuation never gets this
            # fallback and therefore cannot falsely declare completion.
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
            # A planner may propose completion, but it is never the authority
            # that grants it. The service records the proposal and runs a
            # separate completion verifier below.
            return controller.propose_completion(plan, reason=reason), None
        return dict(plan), error or "planner did not produce a next work item"

    async def _verify_self_host_performance(
        self,
        *,
        bundle: SelfHostGateBundle,
        task_id: str | None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Run the complete performance matrix before mission completion.

        Candidate verification remains diff-targeted. Mission completion is a
        separate boundary, so it pays for all three performance proofs against
        a disposable view of the current promoted source.
        """
        from athena.protocol.tasks import Criterion, VerificationSpec, VerificationType
        from athena.self_host.gates import SelfHostGatePolicy
        from athena.verification.identity import command_proof_id

        verifier = self._svc._acceptance_verifier
        commands = SelfHostGatePolicy.all_performance_commands()
        if verifier is None:
            return [], "completion requires the service-owned performance verifier"
        if not commands:
            return [], "completion requires the service-owned performance matrix"

        proof_task_id = f"self-host-performance-{task_id or 'completion'}"
        workspace = WorkspaceSpec(
            id="athena-self-completion-performance",
            root=bundle.project_root,
            revision=bundle.source_revision,
            network_policy=NetworkPolicy.DENY,
            mutation_mode=MutationMode.READ_ONLY,
        )
        try:
            environment = self._svc._self_host_verification_environment(
                workspace,
                include_project_root=True,
                include_rust=True,
                task_id=proof_task_id,
            )
            proof_task = TaskSpec(
                id=proof_task_id,
                objective="prove all self-host performance invariants",
                workspace=workspace,
                metadata={"autonomy": AutonomyLevel.CODING.value},
            )
            criteria = tuple(
                Criterion(
                    id=f"self_host_completion_{command_proof_id(command)}",
                    description=command,
                    verification=VerificationSpec(
                        type=VerificationType.COMMAND,
                        command=command,
                    ),
                    required=True,
                )
                for command in commands
            )
            results = await verifier.verify(
                proof_task,
                criteria,
                verification_environment=environment,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return [], f"completion performance proof could not run: {exc}"

        if len(results) != len(commands):
            return [], "completion performance proof returned incomplete results"
        evidence = [
            {
                "proof_id": command_proof_id(command),
                "passed": bool(passed),
            }
            for command, passed in zip(commands, results, strict=True)
        ]
        failed = [str(item["proof_id"]) for item in evidence if not item["passed"]]
        if failed:
            return evidence, "completion performance proofs failed: " + ", ".join(failed)
        return evidence, None

    async def _verify_self_host_completion(
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
        """Verify a planner completion proposal before creating COMPLETE."""
        evidence = plan.get("evidence") or {}
        completed = plan.get("completed_work_items") or ()
        review = release_evidence.get("review")
        if not completed:
            return None, "completion requires at least one promoted work item"
        if str(getattr(current_index, "source_revision", "") or "") != bundle.source_revision:
            return None, "completion source index is not bound to the current authority"
        if str(evidence.get("source_revision") or "") != bundle.source_revision:
            return None, "completion plan has stale source authority"
        if str(evidence.get("design_bundle_hash") or "") != bundle.design_bundle_hash:
            return None, "completion plan has stale design authority"
        if str(evidence.get("gate_bundle_hash") or "") != bundle.gate_bundle_hash:
            return None, "completion plan has stale gate authority"
        if str(evidence.get("base_fingerprint") or "") != current_fingerprint:
            return None, "completion plan fingerprint does not match the current workspace"
        if release_evidence.get("task_status") != "complete":
            return None, "completion requires a complete final proof task"
        if not isinstance(review, Mapping) or review.get("eligible") is not True:
            return None, "completion requires eligible final review evidence"
        if not review.get("certificate_hash"):
            return None, "completion requires a final verification certificate"
        if any(
            not isinstance(item, Mapping)
            or item.get("status") != "completed"
            or not item.get("task_id")
            or not item.get("branch_id")
            or not item.get("certificate_hash")
            for item in completed
        ):
            return None, "completion contains an incomplete promoted work item"

        performance_evidence, performance_error = await self._verify_self_host_performance(
            bundle=bundle,
            task_id=task_id,
        )
        if performance_error:
            return None, performance_error

        proposal = plan.get("completion_proposal") or {}
        context = bundle.retrieve_design_context(
            paths=("SELF_HOSTING.md", "SECURITY.md", "docs/ARCHITECTURE.md"),
            invariants=("mission completion", "proof before promotion", "candidate isolation"),
        )
        prompt = (
            "Verify whether Athena's self-host mission is actually complete. Return JSON only.\n"
            'Schema: {"complete":true|false,"reason":"...",'
            '"missing_obligations":["..."]}\n'
            "The service, not this response, owns the completion transition.\n"
            f"Original mission objective: {str(mission.get('objective') or '')[:4000]}\n"
            f"Planner completion proposal: {json.dumps(proposal, sort_keys=True)[:4000]}\n"
            f"Completed work: {json.dumps(completed, sort_keys=True)[:18000]}\n"
            f"Current source index: {getattr(current_index, 'index_revision', '')}"
            f" / {getattr(current_index, 'source_revision', '')}\n"
            f"Current workspace fingerprint: {current_fingerprint}\n"
            f"Frozen authority: {json.dumps(bundle.to_record(), sort_keys=True)[:6000]}\n"
            f"Final release evidence: {json.dumps(dict(release_evidence), sort_keys=True)[:12000]}\n"
            f"Full completion performance evidence: {json.dumps(performance_evidence, sort_keys=True)}\n"
            f"Frozen contract context:\n{context[:18000]}"
        )
        response = None
        if self._svc._kernel is not None:
            response = await self._svc._kernel.utility_inference(
                system_prompt=(
                    "You are Athena's completion-verifier role. Evaluate the original "
                    "mission against the supplied durable evidence and frozen contracts. "
                    "Do not invent missing proof and do not approve mutations."
                ),
                user_prompt=prompt,
                role="completion_verifier",
                task_id=task_id,
                metadata={"purpose": "self_host_completion_verification"},
            )
        verdict = parse_self_host_completion_verdict(response)
        if verdict is None:
            return None, "completion verifier returned invalid JSON"
        if verdict["complete"] is not True:
            missing = ", ".join(verdict["missing_obligations"][:8])
            return None, verdict[
                "reason"
            ] or missing or "completion verifier did not prove the objective"
        verification = {
            "role": "completion_verifier",
            "complete": True,
            "reason": verdict["reason"],
            "missing_obligations": verdict["missing_obligations"],
            "performance_proofs": performance_evidence,
        }
        if self._svc._hermes_supervision_active:
            hermes = await self._run_hermes_mission_referee(
                mission,
                plan=plan,
                current_index=current_index,
                bundle=bundle,
                current_fingerprint=current_fingerprint,
                release_evidence=release_evidence,
                task_id=task_id,
            )
            verification["hermes"] = hermes
            if (
                self._svc._hermes_supervision_mode is HermesSupervisionMode.REQUIRED
                and hermes.get("decision") != HermesDecision.MISSION_COMPLETE_SUPPORTED.value
            ):
                return None, str(
                    hermes.get("rationale") or "Hermes did not support mission completion"
                )
        proof = SelfHostMissionController.completion_proof(
            plan,
            objective=str(mission.get("objective") or ""),
            reason=verdict["reason"] or str(proposal.get("reason") or "verified completion"),
            authority=bundle.to_record(),
            base_fingerprint=current_fingerprint,
            release_evidence=release_evidence,
            completion_verification=verification,
        )
        return proof, None

    async def _run_hermes_mission_referee(
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
        """Referee the whole mission without granting mutation authority."""
        referee = self._svc._hermes_referee
        if referee is None:
            return HermesVerdict(
                decision=HermesDecision.HOLD,
                rationale="Hermes referee is not configured",
            ).to_record()
        try:
            completed = plan.get("completed_work_items") or ()
            context = bundle.retrieve_design_context(
                paths=("SELF_HOSTING.md", "SECURITY.md", "docs/ARCHITECTURE.md"),
                invariants=("mission completion", "proof before promotion"),
            )
            packet = ReviewPacket(
                kind="mission",
                mission={
                    "id": mission.get("id"),
                    "objective": mission.get("objective"),
                    "status": mission.get("status"),
                },
                work_item={"completed_work_items": list(completed)},
                risk={"level": "medium", "paths": []},
                base_identity={
                    "fingerprint": current_fingerprint,
                    "source_revision": bundle.source_revision,
                },
                candidate_identity={"fingerprint": current_fingerprint},
                frozen_contract_context=context,
                release_results={
                    **dict(release_evidence),
                    "review_eligible": (
                        release_evidence.get("review", {}).get("eligible") is True
                        if isinstance(release_evidence.get("review"), Mapping)
                        else False
                    ),
                },
                reviewer_history=tuple(
                    dict(item) for item in completed if isinstance(item, Mapping)
                ),
                resource_usage={
                    "current_index_revision": getattr(current_index, "index_revision", ""),
                    "task_id": task_id,
                },
            )
            verdict = await referee.review(packet)
            return verdict.to_record()
        except Exception as exc:
            return HermesVerdict(
                decision=HermesDecision.HOLD,
                rationale=f"Hermes mission packet construction failed: {exc}",
                blockers=("Hermes mission review unavailable",),
            ).to_record()

    async def self_host_status(self, *, workspace_root: str | None = None) -> list[dict[str, Any]]:
        missions = self._svc._self_host_missions
        if missions is None:
            raise RuntimeError("self-host mission store is not started")
        root = str(Path(workspace_root or os.getcwd()).resolve())
        records = await missions.list_recent(root)
        for record in records:
            task_id = record.get("current_task_id")
            if task_id:
                record["candidate"] = await self._svc.operator_candidate(str(task_id))
        return records

    async def continue_self_host(
        self,
        *,
        workspace_root: str | None = None,
        mission_id: str | None = None,
    ) -> dict[str, Any]:
        missions = self._svc._self_host_missions
        tm = self._svc._require_task_manager()
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
        candidate = await self._svc.operator_candidate(task_id) if task_id else None
        if candidate is not None:
            if candidate.get("status") == "VERIFIED":
                await missions.update(mission["id"], status="review")
            return {"status": "review", "mission": mission, "candidate": candidate}
        task = (
            await self._svc._store_tasks.get(task_id)
            if self._svc._store_tasks and task_id
            else None
        )
        task_status = str((task or {}).get("status") or "")
        if task_status in {"queued", "running", "interrupted"}:
            if task_status == "interrupted":
                await tm.enqueue(task_id)
            return {"status": "resumed", "mission": mission, "task_id": task_id}
        if task_status in {"complete", "failed", "cancelled", "blocked", "partial"}:
            expected = str(mission.get("current_base_fingerprint") or "")
            actual = await self._svc.shadow_engine().workspace_fingerprint(root)
            if expected and actual != expected:
                error = (
                    "MISSION STALE: workspace fingerprint changed outside the promoted "
                    "mission state"
                )
                await missions.update(mission["id"], status="blocked", last_error=error)
                return {"status": "stale", "mission": mission, "error": error}
            plan = dict(mission.get("plan") or {})
            if task_status == "complete" and str(mission.get("status") or "") in {
                "promoted",
                "complete",
            }:
                coordinator = self._svc._project_index_coordinator
                if coordinator is None:
                    error = "self-host project index is not started"
                    await missions.update(mission["id"], status="blocked", last_error=error)
                    return {"status": "blocked", "mission": mission, "error": error}
                try:
                    index = await coordinator.current(
                        root,
                        refresh=True,
                        freshness="source_verified",
                    )
                    bundle = SelfHostGateBundle.capture(root, allow_dirty=True)
                    plan, planning_error = await self._plan_next_self_host_item(
                        mission,
                        plan=plan,
                        current_index=index,
                        bundle=bundle,
                        task_id=task_id or None,
                        current_release_evidence={
                            "task_status": task_status,
                            "base_fingerprint": actual,
                            "review": plan.get("review"),
                            "unresolved_failures": (
                                mission.get("last_error")
                                or (task or {}).get("error")
                                or (task or {}).get("reason")
                            ),
                        },
                    )
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    planning_error = f"self-host PLAN NEXT failed: {exc}"
                if planning_error:
                    await missions.update(
                        mission["id"],
                        status="blocked",
                        last_error=planning_error,
                        plan=plan,
                    )
                    return {"status": "blocked", "mission": mission, "error": planning_error}
                if plan.get("phase") == "COMPLETION_PROPOSED":
                    release_evidence = {
                        "task_status": task_status,
                        "base_fingerprint": actual,
                        "review": plan.get("review"),
                        "unresolved_failures": (
                            mission.get("last_error")
                            or (task or {}).get("error")
                            or (task or {}).get("reason")
                        ),
                    }
                    completion_proof, completion_error = await self._verify_self_host_completion(
                        mission,
                        plan=plan,
                        current_index=index,
                        bundle=bundle,
                        current_fingerprint=actual,
                        release_evidence=release_evidence,
                        task_id=task_id or None,
                    )
                    if completion_error:
                        plan["completion_verification"] = {
                            "status": "hold",
                            "error": completion_error,
                        }
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
                        planning_error = (
                            "stored completion lacks service-owned verification evidence"
                        )
                        await missions.update(
                            mission["id"], status="blocked", last_error=planning_error, plan=plan
                        )
                        return {"status": "blocked", "mission": mission, "error": planning_error}
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
            created = await self.submit_self_host(
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
        return {"status": "pending", "mission": mission, "task_id": task_id}
