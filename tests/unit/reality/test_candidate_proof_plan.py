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


async def test_installed_profile_tools_do_not_add_acceptance_to_read_only_work():
    calls = []

    async def source(_task):
        calls.append(1)
        return {"commands": {"python": ("pytest", "ruff check", "mypy")}}

    service = CandidateVerificationService(
        candidate_verifier=_NeverVerifier(), default_criteria_source=source
    )
    task = TaskSpec(id="read-only", objective="read the README and report it")
    assert await service.criteria_for(task) == ()
    assert calls == []


async def test_installed_profile_tools_do_not_add_acceptance_to_execution_only_work():
    service = CandidateVerificationService(
        candidate_verifier=_NeverVerifier(),
        default_criteria_source=lambda _task: {"commands": {"python": ("pytest", "ruff check")}},
    )
    task = TaskSpec(id="execute-only", objective="run the focused test command")
    assert await service.criteria_for(task) == ()


async def test_changed_mutation_work_receives_evidence_based_baseline():
    service = CandidateVerificationService(
        candidate_verifier=_NeverVerifier(),
        default_criteria_source=lambda _task: {
            "commands": {"python": ("pytest",)},
        },
    )
    task = TaskSpec(id="mutation", objective="fix the failing test in the source file")
    criteria = await service.criteria_for(task, changed_resources=("src/app.py",))
    assert [c.verification.command for c in criteria] == ["pytest"]
