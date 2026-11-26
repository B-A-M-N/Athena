"""Runtime execution plans must raise the independent-proof floor."""

from __future__ import annotations


from athena.protocol.tasks import (
    MutationMode,
    SpeculationDepth,
    TaskExecutionPlan,
    TaskSpec,
    VerificationStrength,
    WorkClass,
)
from athena.reality.candidate_verification import CandidateVerificationService


class _Verifier:
    async def verify_against(self, task, criteria, workspace):
        return [{"id": criterion.id, "passed": True} for criterion in criteria]


def _task(work_class: WorkClass) -> TaskSpec:
    return TaskSpec(
        id="plan-task",
        objective="complete the work",
        execution_plan=TaskExecutionPlan(
            work_class=work_class,
            speculation_depth=SpeculationDepth.SINGLE_CANDIDATE,
            isolation_floor=MutationMode.SPECULATIVE,
            verification_floor=VerificationStrength.STRONG,
        ),
    )


def test_typed_complex_plan_requires_independent_proof():
    assert CandidateVerificationService._requires_independent_proof(_task(WorkClass.COMPLEX_CODING))


def test_runtime_escalated_simple_work_inherits_proof_floor():
    service = CandidateVerificationService(candidate_verifier=_Verifier())
    escalated = TaskSpec(
        id="escalated-task",
        objective="small helper",
        execution_plan=TaskExecutionPlan(
            work_class=WorkClass.SIMPLE_EDIT,
            speculation_depth=SpeculationDepth.SINGLE_CANDIDATE,
            isolation_floor=MutationMode.SPECULATIVE,
            verification_floor=VerificationStrength.STRONG,
        ),
    )
    assert not service._requires_independent_proof(escalated)
    # Runtime escalation raises the effective work class and proof floor on
    # durable task state before certification.
    escalated = TaskSpec(
        id="escalated-task",
        objective="small helper",
        execution_plan=TaskExecutionPlan(
            work_class=WorkClass.COMPLEX_CODING,
            speculation_depth=SpeculationDepth.SINGLE_CANDIDATE,
            isolation_floor=MutationMode.SPECULATIVE,
            verification_floor=VerificationStrength.STRONG,
        ),
    )
    assert service._requires_independent_proof(escalated)
