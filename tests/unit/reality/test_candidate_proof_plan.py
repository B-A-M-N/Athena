"""Complex proof-planning failures are typed, not fake failing commands."""

from __future__ import annotations

from athena.protocol.tasks import Criterion, TaskSpec, WorkspaceSpec
from athena.reality.candidate_verification import CandidateVerificationService


class _NeverVerifier:
    async def verify_against(self, task, criteria, workspace):  # pragma: no cover
        raise AssertionError("planning failure must not reach verifier")


async def test_complex_without_baseline_returns_typed_plan_without_command():
    svc = CandidateVerificationService(candidate_verifier=_NeverVerifier())
    task = TaskSpec(
        id="t",
        objective="complex change",
        acceptance_criteria=(Criterion(id="explicit", description="explicit"),),
        metadata={"_athena_work_class": "complex_coding"},
    )
    plan = await svc.proof_plan(task, workspace=WorkspaceSpec(id="w", root="/tmp"))
    assert plan.criteria == ()
    assert plan.planning_errors
    assert "false" not in " ".join(plan.planning_errors)
    assert plan.required_strength == "strong"


async def test_criteria_for_omits_fake_false_command():
    svc = CandidateVerificationService(candidate_verifier=_NeverVerifier())
    task = TaskSpec(
        id="t",
        objective="complex change",
        acceptance_criteria=(Criterion(id="explicit", description="explicit"),),
        metadata={"_athena_work_class": "complex_coding"},
    )
    criteria = await svc.criteria_for(task, workspace=WorkspaceSpec(id="w", root="/tmp"))
    assert criteria == ()
