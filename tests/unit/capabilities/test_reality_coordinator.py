"""Acceptance tests for the RealityCoordinator completion authority.

These prove the end-to-end lifecycle: a speculative task's candidate edits
only become real reality after verification binds to the branch, a certificate
is issued, and the branch commits.  They also prove the negative cases:
failed/cancelled tasks leave the real workspace byte-identical and discard
their candidate.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from athena.capabilities.dispatcher import CapabilityDispatcher
from athena.capabilities.fs import FilesystemCapability
from athena.capabilities.registry import CapabilityRegistry
from athena.kernel.termination import TerminationDecision
from athena.policy.engine import PolicyEngine
from athena.protocol.capabilities import (
    DispatchDirectives,
    EffectClass,
)
from athena.protocol.tasks import (
    AgentRequest,
    AutonomyLevel,
    Criterion,
    MutationMode,
    TaskSpec,
    TaskStatus,
    VerificationSpec,
    VerificationType,
    WorkspaceSpec,
)
from athena.reality import (
    RealityCoordinator,
    ShadowCandidateVerifier,
)
from athena.shadow.engine import ShadowEngine
from athena.service.service import AthenaService
from athena.causal.checkpoint import CheckpointManager


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "README.txt").write_text("base\n", encoding="utf-8")
    return project


def _workspace(project: Path) -> WorkspaceSpec:
    return WorkspaceSpec(
        id="project",
        root=str(project),
        mutation_mode=MutationMode.SPECULATIVE,
    )


def _spec(workspace: WorkspaceSpec, criteria: list[Criterion] | None = None) -> TaskSpec:
    return TaskSpec(
        id="task-lifecycle",
        objective="patch README",
        session_id="session-lifecycle",
        acceptance_criteria=tuple(criteria or []),
        workspace=workspace,
        metadata={"autonomy": "autonomous"},
    )


def _gate_engine(tmp_path: Path):
    engine = ShadowEngine(
        roots_parent=str(tmp_path / "shadows"),
        state_root=str(tmp_path / "state"),
    )
    registry = CapabilityRegistry()
    registry.register(FilesystemCapability())
    dispatcher = CapabilityDispatcher(registry, PolicyEngine(profile="offline"))
    from athena.reality import RealityGate

    gate = RealityGate(engine)
    engine.bind(dispatcher)
    dispatcher.set_reality_gate(gate)
    return gate, engine, dispatcher


def _coordinator(gate, engine, dispatcher):
    from athena.kernel.verifiers import CompositeVerifier

    verifier = CompositeVerifier(dispatcher=dispatcher)
    return RealityCoordinator(
        shadow_engine=engine,
        reality_gate=gate,
        candidate_verifier=ShadowCandidateVerifier(verifier),
    )


async def test_explicit_acceptance_criteria_augment_baseline_plan(tmp_path):
    project = _project(tmp_path)
    gate, engine, dispatcher = _gate_engine(tmp_path)
    coordinator = RealityCoordinator(
        shadow_engine=engine,
        reality_gate=gate,
        candidate_verifier=ShadowCandidateVerifier(
            __import__("athena.kernel.verifiers", fromlist=["CompositeVerifier"]).CompositeVerifier(
                dispatcher=dispatcher
            )
        ),
        default_criteria_source=lambda _task: {
            "commands": {"test": ["pytest"], "lint": ["ruff check"]}
        },
    )
    explicit = Criterion(
        id="file-proof",
        description="the edited file contains the marker",
        verification=VerificationSpec(
            type=VerificationType.FILE,
            path="README.txt",
            predicate="contains:patched",
        ),
    )
    task = _spec(_workspace(project), [explicit])

    criteria = await coordinator._criteria_for(  # noqa: SLF001 - contract-focused test
        task,
        workspace=task.workspace,
        changed_resources=("app.py", "pyproject.toml"),
        impact={"build": True, "index_revision": "index-1"},
    )

    ids = [criterion.id for criterion in criteria]
    assert ids[0] == "file-proof"
    assert any(
        criterion.verification and criterion.verification.command == "pytest"
        for criterion in criteria
    )
    assert (
        sum(
            criterion.verification is not None and criterion.verification.command == "pytest"
            for criterion in criteria
        )
        == 1
    )
    assert coordinator._plans[task.id]["required_strength"] == "strong"  # noqa: SLF001


async def test_simple_patch_reaches_real_workspace_only_after_verification(tmp_path):
    project = _project(tmp_path)
    ws = _workspace(project)
    gate, engine, dispatcher = _gate_engine(tmp_path)
    coordinator = _coordinator(gate, engine, dispatcher)

    # Simulate the kernel opening a branch and mutating it.
    route = await gate.route(
        __import__(
            "athena.protocol.capabilities", fromlist=["CapabilityRequest"]
        ).CapabilityRequest(
            capability_id="fs",
            arguments={"operation": "write", "path": "README.txt", "content": "patched\n"},
            task_id="task-lifecycle",
            call_id="write-1",
        ),
        ws,
        {EffectClass.WRITE_LOCAL},
        FilesystemCapability().descriptor,
    )
    assert route.disposition.value == "speculative"
    branch = gate.active_branch("task-lifecycle")
    assert branch is not None

    # Mutate the candidate shadow directly (the executor would do this).
    shadow_readme = Path(branch.shadow_workspace.root) / "README.txt"
    shadow_readme.write_text("patched\n", encoding="utf-8")

    # Real workspace is still base.
    assert (project / "README.txt").read_text(encoding="utf-8") == "base\n"

    criteria = [
        Criterion(
            id="readme_patched",
            description="README contains patched",
            verification=VerificationSpec(
                type=VerificationType.FILE,
                path="README.txt",
                predicate="contains:patched",
            ),
            required=True,
        ),
    ]
    spec = _spec(ws, criteria)
    decision = TerminationDecision(
        terminal=True,
        status=TaskStatus.COMPLETE,
        reason="objective satisfied",
    )

    result = await coordinator.prepare_completion(spec, decision)
    assert result.committed is True
    assert result.decision.status is TaskStatus.COMPLETE
    assert result.certificate is not None

    # NOW the real workspace has the candidate content.
    assert (project / "README.txt").read_text(encoding="utf-8") == "patched\n"
    assert result.branch_id is not None
    committed_branch = engine.get_branch(result.branch_id)
    assert committed_branch is not None
    assert Path(committed_branch.shadow_workspace.root).is_dir()

    await coordinator.mark_finalized(spec.id)
    assert not Path(committed_branch.shadow_workspace.root).exists()


async def test_failed_speculative_task_leaves_real_workspace_byte_identical(tmp_path):
    project = _project(tmp_path)
    ws = _workspace(project)
    gate, engine, dispatcher = _gate_engine(tmp_path)
    coordinator = _coordinator(gate, engine, dispatcher)

    await gate.route(
        __import__(
            "athena.protocol.capabilities", fromlist=["CapabilityRequest"]
        ).CapabilityRequest(
            capability_id="fs",
            arguments={"operation": "write", "path": "README.txt", "content": "failed\n"},
            task_id="task-lifecycle",
            call_id="write-1",
        ),
        ws,
        {EffectClass.WRITE_LOCAL},
        FilesystemCapability().descriptor,
    )
    branch = gate.active_branch("task-lifecycle")
    shadow_readme = Path(branch.shadow_workspace.root) / "README.txt"
    shadow_readme.write_text("failed\n", encoding="utf-8")

    # A non-complete terminal status must discard the candidate.
    await coordinator.discard_incomplete("task-lifecycle", TaskStatus.FAILED)

    assert (project / "README.txt").read_text(encoding="utf-8") == "base\n"
    assert gate.active_branch("task-lifecycle") is None


async def test_file_criterion_reads_candidate_not_base(tmp_path):
    project = _project(tmp_path)
    ws = _workspace(project)
    gate, engine, dispatcher = _gate_engine(tmp_path)
    coordinator = _coordinator(gate, engine, dispatcher)

    await gate.route(
        __import__(
            "athena.protocol.capabilities", fromlist=["CapabilityRequest"]
        ).CapabilityRequest(
            capability_id="fs",
            arguments={"operation": "write", "path": "new_file.py", "content": "x = 1\n"},
            task_id="task-lifecycle",
            call_id="write-1",
        ),
        ws,
        {EffectClass.WRITE_LOCAL},
        FilesystemCapability().descriptor,
    )
    branch = gate.active_branch("task-lifecycle")
    (Path(branch.shadow_workspace.root) / "new_file.py").write_text("x = 1\n", encoding="utf-8")

    # The real workspace does NOT contain new_file.py.
    assert not (project / "new_file.py").exists()

    criteria = [
        Criterion(
            id="new_file_exists",
            description="new_file.py exists",
            verification=VerificationSpec(
                type=VerificationType.FILE,
                path="new_file.py",
                predicate="exists",
            ),
            required=True,
        ),
    ]
    spec = _spec(ws, criteria)
    decision = TerminationDecision(
        terminal=True,
        status=TaskStatus.COMPLETE,
        reason="objective satisfied",
    )

    result = await coordinator.prepare_completion(spec, decision)
    # Verification passed against the candidate, and the commit promoted it.
    assert result.committed is True
    assert (project / "new_file.py").exists()


async def test_self_host_retains_verified_candidate_until_operator_apply(tmp_path):
    project = _project(tmp_path)
    ws = _workspace(project)
    gate, engine, dispatcher = _gate_engine(tmp_path)
    events = []

    async def sink(event):
        events.append(event)

    class _PassingVerifier:
        async def verify_against(self, task, criteria, workspace):
            return [{"id": criterion.id, "passed": True} for criterion in criteria]

    coordinator = RealityCoordinator(
        shadow_engine=engine,
        reality_gate=gate,
        candidate_verifier=_PassingVerifier(),
        event_sink=sink,
    )
    await gate.route(
        __import__(
            "athena.protocol.capabilities", fromlist=["CapabilityRequest"]
        ).CapabilityRequest(
            capability_id="fs",
            arguments={"operation": "write", "path": "README.txt", "content": "self-hosted\n"},
            task_id="task-lifecycle",
            call_id="write-self",
        ),
        ws,
        {EffectClass.WRITE_LOCAL},
        FilesystemCapability().descriptor,
    )
    branch = gate.active_branch("task-lifecycle")
    assert branch is not None
    criteria = [
        Criterion(
            id="readme_patched",
            description="README contains self-hosted",
            verification=VerificationSpec(
                type=VerificationType.FILE,
                path="README.txt",
                predicate="contains:self-hosted",
            ),
        )
    ]
    spec = replace(
        _spec(ws, criteria),
        metadata={"autonomy": "coding", "_athena_review_before_commit": True},
    )
    (Path(branch.shadow_workspace.root) / "README.txt").write_text(
        "self-hosted\n", encoding="utf-8"
    )

    result = await coordinator.prepare_completion(
        spec,
        TerminationDecision(terminal=True, status=TaskStatus.COMPLETE, reason="ready"),
    )

    assert result.committed is False
    assert result.branch_id == branch.id
    assert gate.active_branch("task-lifecycle") is branch
    assert (project / "README.txt").read_text(encoding="utf-8") == "base\n"
    assert any(event.type == "CandidateReadyForReview" for event in events)


async def test_unverified_candidate_yields_partial_not_complete(tmp_path):
    project = _project(tmp_path)
    ws = _workspace(project)
    gate, engine, dispatcher = _gate_engine(tmp_path)
    coordinator = _coordinator(gate, engine, dispatcher)

    await gate.route(
        __import__(
            "athena.protocol.capabilities", fromlist=["CapabilityRequest"]
        ).CapabilityRequest(
            capability_id="fs",
            arguments={"operation": "write", "path": "README.txt", "content": "done\n"},
            task_id="task-lifecycle",
            call_id="write-1",
        ),
        ws,
        {EffectClass.WRITE_LOCAL},
        FilesystemCapability().descriptor,
    )
    branch = gate.active_branch("task-lifecycle")
    (Path(branch.shadow_workspace.root) / "README.txt").write_text("done\n", encoding="utf-8")

    # Criterion requires content that is NOT in the candidate.
    criteria = [
        Criterion(
            id="requires_tests",
            description="test file exists",
            verification=VerificationSpec(
                type=VerificationType.FILE,
                path="test_foo.py",
                predicate="exists",
            ),
            required=True,
        ),
    ]
    spec = _spec(ws, criteria)
    decision = TerminationDecision(
        terminal=True,
        status=TaskStatus.COMPLETE,
        reason="objective satisfied",
    )

    result = await coordinator.prepare_completion(spec, decision)
    assert result.decision.status is TaskStatus.PARTIAL
    assert result.committed is False
    # Real workspace untouched because the candidate was unverified.
    assert (project / "README.txt").read_text(encoding="utf-8") == "base\n"
    assert gate.active_branch("task-lifecycle") is None
    assert not Path(branch.shadow_workspace.root).exists()


async def test_transactional_candidate_is_compensated_when_verification_fails(tmp_path):
    project = _project(tmp_path)
    ws = _workspace(project)
    gate, engine, dispatcher = _gate_engine(tmp_path)
    gate.bind_checkpoint_manager(CheckpointManager(root=str(tmp_path / "ckpts")))
    coordinator = _coordinator(gate, engine, dispatcher)

    request = __import__(
        "athena.protocol.capabilities", fromlist=["CapabilityRequest"]
    ).CapabilityRequest(
        capability_id="fs",
        arguments={"operation": "write", "path": "README.txt", "content": "changed\n"},
        task_id="task-lifecycle",
        call_id="write-transactional",
    )
    route = await gate.route(
        request,
        ws,
        {EffectClass.WRITE_LOCAL},
        FilesystemCapability().descriptor,
        tier="transactional",
    )
    assert route.disposition.value == "transactional"
    result = await dispatcher.dispatch(
        request,
        workspace=ws,
        profile="autonomous",
        directives=DispatchDirectives(reality_tier="transactional"),
    )
    assert result.status.value == "ok"

    criteria = [
        Criterion(
            id="missing",
            description="missing file exists",
            verification=VerificationSpec(
                type=VerificationType.FILE,
                path="missing.txt",
                predicate="exists",
            ),
        )
    ]
    result = await coordinator.prepare_completion(
        _spec(ws, criteria),
        TerminationDecision(terminal=True, status=TaskStatus.COMPLETE, reason="done"),
    )

    assert result.decision.status is TaskStatus.PARTIAL
    assert (project / "README.txt").read_text(encoding="utf-8") == "base\n"
    assert gate.checkpoint_id("task-lifecycle") is None


async def test_cancelled_task_discards_uncommitted_candidate(tmp_path):
    project = _project(tmp_path)
    ws = _workspace(project)
    gate, engine, dispatcher = _gate_engine(tmp_path)
    coordinator = _coordinator(gate, engine, dispatcher)

    await gate.route(
        __import__(
            "athena.protocol.capabilities", fromlist=["CapabilityRequest"]
        ).CapabilityRequest(
            capability_id="fs",
            arguments={"operation": "write", "path": "README.txt", "content": "wip\n"},
            task_id="task-lifecycle",
            call_id="write-1",
        ),
        ws,
        {EffectClass.WRITE_LOCAL},
        FilesystemCapability().descriptor,
        tier="transactional",
    )
    branch = gate.active_branch("task-lifecycle")
    assert branch is not None

    await coordinator.discard_incomplete("task-lifecycle", TaskStatus.CANCELLED)

    assert gate.active_branch("task-lifecycle") is None
    assert (project / "README.txt").read_text(encoding="utf-8") == "base\n"


def test_coding_tasks_default_to_speculative_workspace():
    service = AthenaService.in_memory()
    spec = service._build_task_spec(
        AgentRequest(prompt="fix the parser", autonomy=AutonomyLevel.CODING),
        "session-lifecycle",
    )
    assert spec.workspace is not None
    assert spec.workspace.mutation_mode is MutationMode.SPECULATIVE


def test_self_host_forces_candidate_boundary_even_for_direct_request():
    service = AthenaService.in_memory()
    with pytest.raises(ValueError, match="reserved Athena metadata"):
        service._build_task_spec(
            AgentRequest(
                prompt="improve Athena",
                autonomy=AutonomyLevel.SUPERVISED,
                workspace=WorkspaceSpec(
                    id="repo",
                    root="/tmp/repo",
                    mutation_mode=MutationMode.DIRECT,
                ),
                metadata={"self_host": True, "mutation_mode": "direct"},
            ),
            "session-self",
        )


async def test_complex_coding_without_derivable_proof_fails_closed(tmp_path):
    """Complex coding cannot synthesize proof from an empty criteria plan."""
    project = _project(tmp_path)
    ws = _workspace(project)
    gate, engine, dispatcher = _gate_engine(tmp_path)
    coordinator = _coordinator(gate, engine, dispatcher)

    await gate.route(
        __import__(
            "athena.protocol.capabilities", fromlist=["CapabilityRequest"]
        ).CapabilityRequest(
            capability_id="fs",
            arguments={"operation": "write", "path": "candidate.py", "content": "x = 1\n"},
            task_id="task-complex-proof",
            call_id="complex-write",
        ),
        ws,
        {EffectClass.WRITE_LOCAL},
        FilesystemCapability().descriptor,
    )
    branch = gate.active_branch("task-complex-proof")
    assert branch is not None
    (Path(branch.shadow_workspace.root) / "candidate.py").write_text("x = 1\n", encoding="utf-8")
    task = replace(
        _spec(ws),
        id="task-complex-proof",
        metadata={"autonomy": "autonomous", "_athena_work_class": "complex_coding"},
    )
    decision = TerminationDecision(
        terminal=True,
        status=TaskStatus.COMPLETE,
        reason="model says the refactor is done",
    )
    result = await coordinator.prepare_completion(task, decision)

    assert result.committed is False
    assert result.decision.status is TaskStatus.PARTIAL
    assert "verification_unavailable" in result.decision.reason
    assert (project / "candidate.py").exists() is False


async def test_strong_plan_requires_all_selected_evidence_classes(tmp_path):
    """A strong plan does not certify when only one category passes."""
    project = _project(tmp_path)
    ws = _workspace(project)
    gate, engine, dispatcher = _gate_engine(tmp_path)

    class _OnePassingVerifier:
        async def verify_against(self, task, criteria, workspace):
            # The planner selected a test and a lint command; only the first
            # test probe actually passes.
            return [True, False]

    coordinator = RealityCoordinator(
        shadow_engine=engine,
        reality_gate=gate,
        candidate_verifier=_OnePassingVerifier(),
        default_criteria_source=lambda _task: {
            "commands": {"test": ["pytest"], "lint": ["ruff check"]}
        },
    )

    await gate.route(
        __import__(
            "athena.protocol.capabilities", fromlist=["CapabilityRequest"]
        ).CapabilityRequest(
            capability_id="fs",
            arguments={"operation": "write", "path": "app.py", "content": "x = 1\n"},
            task_id="task-strong-proof",
            call_id="strong-write",
        ),
        ws,
        {EffectClass.WRITE_LOCAL},
        FilesystemCapability().descriptor,
    )
    branch = gate.active_branch("task-strong-proof")
    assert branch is not None
    (Path(branch.shadow_workspace.root) / "app.py").write_text("x = 1\n", encoding="utf-8")

    task = replace(
        _spec(ws),
        id="task-strong-proof",
        metadata={"autonomy": "autonomous"},
    )
    # Force changed resources so the planner marks the plan strong.
    from dataclasses import replace as dc_replace

    task = dc_replace(task, metadata={**task.metadata, "changed_resources": ("app.py",)})
    results = await coordinator._verify_or_fail(  # noqa: SLF001
        task,
        await coordinator._criteria_for(  # noqa: SLF001
            task,
            workspace=branch.shadow_workspace,
            changed_resources=("app.py", "pyproject.toml"),
            impact={"build": True, "index_revision": "idx-1"},
        ),
        branch.shadow_workspace,
    )

    strength_failures = {
        "required_strength_unsatisfied",
        "verification_strength_unavailable",
    }
    assert any(item.get("id") in strength_failures for item in results)
    assert not all(item.get("passed") for item in results)


async def test_complex_explicit_criteria_without_baseline_fail_closed(tmp_path):
    """Complex coding with explicit criteria but no project baseline must
    refuse to certify. Explicit criteria strengthen proof, never substitute."""
    project = _project(tmp_path)
    ws = _workspace(project)
    gate, engine, dispatcher = _gate_engine(tmp_path)
    coordinator = _coordinator(gate, engine, dispatcher)
    verifier_calls = []

    class TrackingVerifier:
        async def verify(self, task, criteria):
            verifier_calls.append([c.id for c in criteria])
            return [True] * len(criteria)

    from athena.reality import ShadowCandidateVerifier

    coordinator._candidate_verification._candidate_verifier = ShadowCandidateVerifier(
        TrackingVerifier()
    )
    task = replace(
        _spec(ws),
        id="task-explicit-only",
        metadata={"autonomy": "autonomous", "_athena_work_class": "complex_coding"},
        acceptance_criteria=(
            Criterion(
                id="file-exists",
                description="output file exists",
                verification=VerificationSpec(
                    type=VerificationType.FILE,
                    path="output.txt",
                ),
            ),
        ),
    )
    plan = await coordinator._candidate_verification.proof_plan(task, workspace=task.workspace)
    assert plan.criteria == ()
    assert plan.planning_errors
    # Verifier was never called because proof was refused at planning.
    assert verifier_calls == []


async def test_verification_strength_result_is_recorded(tmp_path):
    """Requested vs achieved strength is a structured result on the service."""
    from athena.reality.candidate_verification import CandidateVerificationService

    class Verifier:
        async def verify_against(self, task, criteria, workspace):
            return [{"id": c.id, "passed": True} for c in criteria]

    svc = CandidateVerificationService(candidate_verifier=Verifier())
    task_id = "t-strength"
    svc._plans[task_id] = {
        "required_strength": "strong",
        "criterion_categories": {
            "project_default:1": "test",
            "project_default:2": "lint",
        },
    }
    criteria = (
        Criterion(
            id="project_default:1",
            description="test probe",
            verification=VerificationSpec(type=VerificationType.COMMAND, command="true"),
        ),
        Criterion(
            id="project_default:2",
            description="lint probe",
            verification=VerificationSpec(type=VerificationType.COMMAND, command="true"),
        ),
    )
    task = replace(_spec(_workspace(tmp_path)), id=task_id)
    results = await svc.verify_or_fail(task, criteria, task.workspace)
    assert all(r["passed"] for r in results)
    strength = svc.strength_for(task_id)
    assert strength["requested_strength"] == "strong"
    assert strength["achieved_strength"] == "strong"
    assert set(strength["passed_categories"]) == {"test", "lint"}
    assert strength["unavailable_categories"] == []
