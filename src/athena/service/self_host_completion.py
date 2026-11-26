"""Completion-proof mechanics for the service-owned self-host mission.

This module is subordinate to :class:`SelfHostService`. It proves the
completion proposal against frozen mission authority, executable performance
criteria, and optional Hermes review; it never changes mission status or
promotes a candidate.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from athena.hermes import HermesDecision, HermesVerdict, ReviewPacket
from athena.protocol.tasks import (
    AutonomyLevel,
    Criterion,
    MutationMode,
    NetworkPolicy,
    TaskSpec,
    VerificationSpec,
    VerificationType,
    WorkspaceSpec,
)
from athena.self_host.controller import SelfHostMissionController
from athena.self_host.gates import SelfHostGateBundle, SelfHostGatePolicy
from athena.service._parsing import parse_self_host_completion_verdict
from athena.service.config import HermesSupervisionMode
from athena.verification.identity import command_proof_id

__all__ = ["SelfHostCompletionService"]


class SelfHostCompletionService:
    """Prove a self-host mission's completion proposal."""

    def __init__(self, ports: Any, *, verification_environment: Callable[..., Any]) -> None:
        self._ports = ports
        self._verification_environment = verification_environment

    async def verify_performance(
        self,
        *,
        bundle: SelfHostGateBundle,
        task_id: str | None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Run the complete performance matrix before mission completion."""
        verifier = self._ports.acceptance_verifier
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
            environment = self._verification_environment(
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
            {"proof_id": command_proof_id(command), "passed": bool(passed)}
            for command, passed in zip(commands, results, strict=True)
        ]
        failed = [str(item["proof_id"]) for item in evidence if not item["passed"]]
        if failed:
            return evidence, "completion performance proofs failed: " + ", ".join(failed)
        return evidence, None

    async def verify_completion(
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

        performance_evidence, performance_error = await self.verify_performance(
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
            '{"complete":true|false,"reason":"...","missing_obligations":["..."]}\n'
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
        if self._ports.kernel is not None:
            response = await self._ports.kernel.utility_inference(
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
        if self._ports.hermes_supervision_active:
            hermes = await self.run_hermes_referee(
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
                self._ports.hermes_supervision_mode is HermesSupervisionMode.REQUIRED
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

    async def run_hermes_referee(
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
        referee = self._ports.hermes_referee
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
