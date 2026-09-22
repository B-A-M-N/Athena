"""Trusted execution plans are persisted authority, not caller suggestions."""

from __future__ import annotations


from athena.protocol.tasks import (
    MutationMode,
    SpeculationDepth,
    TaskExecutionPlan,
    TaskSpec,
    VerificationStrength,
    WorkClass,
)
from athena.service.task_intake import TaskIntake


def _authority_plan(**overrides):
    values = {
        "work_class": WorkClass.COMPLEX_CODING,
        "speculation_depth": SpeculationDepth.MULTI_CANDIDATE,
        "isolation_floor": MutationMode.SPECULATIVE,
        "verification_floor": VerificationStrength.STRONG,
    }
    values.update(overrides)
    return TaskExecutionPlan(**values)


def test_trusted_spec_preserves_persisted_complex_authority():
    spec = TaskSpec(
        id="task-authority",
        objective="tell me a joke",
        execution_plan=_authority_plan(),
    )
    normalized = TaskIntake.normalize_spec(spec, trusted=True)
    assert normalized.execution_plan == spec.execution_plan
    assert normalized.metadata["_athena_work_class"] == "complex_coding"
    assert normalized.metadata["_athena_speculation_depth"] == "multi_candidate"


def test_trusted_legacy_record_derives_once_from_objective():
    spec = TaskSpec(id="task-legacy", objective="refactor the subsystem and run all tests")
    normalized = TaskIntake.normalize_spec(spec, trusted=True)
    assert normalized.execution_plan.work_class is WorkClass.COMPLEX_CODING
    assert normalized.execution_plan.verification_floor is VerificationStrength.STRONG


def test_untrusted_caller_authority_is_ignored():
    spec = TaskSpec(
        id="task-untrusted",
        objective="tell me a joke",
        execution_plan=_authority_plan(),
    )
    normalized = TaskIntake.normalize_spec(spec, trusted=False)
    assert normalized.execution_plan.work_class is WorkClass.NON_CODING
    assert normalized.execution_plan.verification_floor is VerificationStrength.NONE


def test_current_safety_rules_strengthen_but_never_downgrade():
    spec = TaskSpec(
        id="task-merge",
        objective="refactor the authentication subsystem and run all tests",
        execution_plan=_authority_plan(
            work_class=WorkClass.SIMPLE_EDIT,
            speculation_depth=SpeculationDepth.SINGLE_CANDIDATE,
            verification_floor=VerificationStrength.STANDARD,
        ),
    )
    normalized = TaskIntake.normalize_spec(spec, trusted=True)
    assert normalized.execution_plan.work_class is WorkClass.COMPLEX_CODING
    assert normalized.execution_plan.speculation_depth is SpeculationDepth.SINGLE_CANDIDATE
    assert normalized.execution_plan.verification_floor is VerificationStrength.STRONG
    assert normalized.execution_plan.isolation_floor is MutationMode.SPECULATIVE
