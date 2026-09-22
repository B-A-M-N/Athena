"""Comparison lifecycle mechanism subordinate to FusionOrchestrator.

Owns how verified alternatives are classified and retained; the orchestrator
remains the single entrypoint and never delegates authority to this helper.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from athena.fusion.orchestrator import FusionOrchestrator

__all__ = ["ComparisonLifecycle"]


class ComparisonLifecycle:
    """Classify candidates and persist exact selection evidence."""

    def __init__(self, orchestrator: "FusionOrchestrator") -> None:
        self._orchestrator = orchestrator

    async def compare(
        self,
        *,
        task_id: str,
        proposals: list[list[dict]],
        invariants: list[dict] | None,
        profile: str | None,
    ) -> dict[str, Any]:
        orch = self._orchestrator
        if len(proposals) < 2:
            raise ValueError("compare requires at least two proposals")
        if len(proposals) > 8:
            raise ValueError("compare accepts at most eight proposals")

        candidates: list[dict[str, Any]] = []
        for index, proposal in enumerate(proposals):
            if not proposal:
                candidates.append(
                    {
                        "candidate_index": index,
                        "status": "FAILED",
                        "verified": False,
                        "error": "proposal must be non-empty",
                    }
                )
                continue
            outcome = await orch.run_experiment(
                task_id=task_id,
                proposal=proposal,
                invariants=invariants,
                profile=profile,
                auto_fork_on_failure=False,
            )
            candidates.append({"candidate_index": index, **dataclasses.asdict(outcome)})

        attempted: list[str] = []
        verified: list[str] = []
        failed: list[str] = []
        certificates: dict[str, dict] = {}
        for record in candidates:
            branch_id = record.get("branch_id")
            if not branch_id:
                continue
            attempted.append(str(branch_id))
            if not record.get("verified"):
                failed.append(str(branch_id))
                continue
            branch = orch.shadow.get_branch(str(branch_id))
            certificate = getattr(branch, "verification_certificate", None)
            to_record = getattr(certificate, "to_record", None)
            certificate_record = to_record() if callable(to_record) else dict(certificate or {})
            if not certificate_record:
                failed.append(str(branch_id))
                continue
            verified.append(str(branch_id))
            certificates[str(branch_id)] = {
                "branch_id": branch_id,
                "certificate_hash": certificate_record.get("certificate_hash"),
                "certificate_id": certificate_record.get("certificate_id"),
                "candidate_fingerprint": certificate_record.get("candidate_fingerprint"),
                "proof_plan_id": certificate_record.get("proof_plan_id"),
                "record": certificate_record,
            }
        comparison = orch.selection_store.create(
            task_id=task_id,
            candidate_branch_ids=attempted,
            verified_branch_ids=verified,
            verification_certificates=certificates,
        )
        return {
            "status": "COMPLETED",
            "task_id": task_id,
            "candidate_count": len(candidates),
            "verified_count": len(verified),
            "candidates": candidates,
            "comparison_id": comparison.comparison_id,
            "selection": "kernel_decision_required",
            "reality_mutated": False,
        }

    async def select_candidate(self, comparison_id: str, branch_id: str) -> dict:
        orch = self._orchestrator
        record = orch.selection_store.select(comparison_id, branch_id)
        branch = orch.shadow.get_branch(branch_id)
        if branch is None or branch.task_id != record.task_id:
            raise RuntimeError(
                f"selection_integrity_error: selected branch {branch_id} is unavailable"
            )
        if branch.status != "VERIFIED":
            raise RuntimeError(
                f"selection_integrity_error: selected branch {branch_id} is "
                f"{branch.status}, not VERIFIED"
            )
        expected_fingerprint = record.selected_branch_fingerprint
        if expected_fingerprint:
            live_certificate = branch.verification_certificate
            to_record = getattr(live_certificate, "to_record", None)
            live_record = to_record() if callable(to_record) else dict(live_certificate or {})
            if live_record.get("candidate_fingerprint") != expected_fingerprint:
                raise RuntimeError(
                    "selection_integrity_error: selected branch fingerprint no longer "
                    "matches the comparison certificate"
                )
        discarded: list[dict] = []
        for rejected_id in record.rejected_branch_ids:
            rejected = orch.shadow.get_branch(rejected_id)
            if rejected is not None and rejected.status == "VERIFIED":
                outcome = await orch.shadow.discard(
                    rejected, reason=f"lost comparison {comparison_id}"
                )
                discarded.append({"branch_id": rejected_id, "status": outcome.get("status")})
        return {
            "comparison_id": comparison_id,
            "selected_branch_id": record.selected_branch_id,
            "rejected_branch_ids": record.rejected_branch_ids,
            "discarded": discarded,
            "lifecycle": record.lifecycle,
        }
