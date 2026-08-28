"""P0 reality-integrity contracts for mutation CAS and completion state."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from athena.capabilities.dispatcher import CapabilityDispatcher
from athena.capabilities.fs import FilesystemCapability
from athena.capabilities.registry import CapabilityRegistry
from athena.causal.checkpoint import CheckpointManager
from athena.kernel.termination import TerminationDecision
from athena.policy.engine import PolicyEngine
from athena.protocol.capabilities import (
    CapabilityRequest,
    DispatchDirectives,
    EffectClass,
    InvocationContext,
)
from athena.protocol.tasks import (
    Criterion,
    MutationMode,
    TaskSpec,
    TaskStatus,
    VerificationSpec,
    VerificationType,
    WorkspaceSpec,
)
from athena.reality import RealityCoordinator, TransactionRecoveryRequired
from athena.reality.completion import CompletionJournal
from athena.reality.gate import RealityGate
from athena.state.rollback import RollbackExecutor
from athena.shadow.engine import ShadowEngine, VerificationCertificate


@pytest.mark.asyncio
async def test_internal_preimage_cas_refuses_stale_write(tmp_path: Path):
    target = tmp_path / "state.txt"
    target.write_text("base\n", encoding="utf-8")
    workspace = WorkspaceSpec(id="repo", root=str(tmp_path))
    request = CapabilityRequest(
        capability_id="fs",
        arguments={"operation": "write", "path": "state.txt", "content": "new\n"},
        task_id="task-cas",
        call_id="call-cas",
    )
    import hashlib

    expected = hashlib.sha256(b"base\n").hexdigest()
    target.write_text("external\n", encoding="utf-8")
    result = await FilesystemCapability().invoke(
        request,
        context=InvocationContext(
            workspace=workspace,
            directives=DispatchDirectives(
                expected_preimages={str(target): expected},
            ),
        ),
    )
    assert result.status.value == "failed"
    assert target.read_text(encoding="utf-8") == "external\n"


@pytest.mark.asyncio
async def test_transaction_compensation_refuses_unrelated_drift(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    target = project / "state.txt"
    target.write_text("base\n", encoding="utf-8")
    workspace = WorkspaceSpec(
        id="repo",
        root=str(project),
        mutation_mode=MutationMode.SPECULATIVE,
    )
    shadow = ShadowEngine(
        roots_parent=str(tmp_path / "shadows"),
        state_root=str(tmp_path / "state"),
    )
    registry = CapabilityRegistry()
    registry.register(FilesystemCapability())
    dispatcher = CapabilityDispatcher(registry, PolicyEngine(profile="offline"))
    shadow.bind(dispatcher)
    gate = RealityGate(
        shadow,
        checkpoint_manager=CheckpointManager(
            root=str(tmp_path / "checkpoints"),
        ),
    )
    request = CapabilityRequest(
        capability_id="fs",
        arguments={"operation": "write", "path": "state.txt", "content": "owned\n"},
        task_id="task-drift",
        call_id="call-drift",
    )
    await gate.route(
        request,
        workspace,
        {EffectClass.WRITE_LOCAL},
        FilesystemCapability().descriptor,
        tier="transactional",
    )
    target.write_text("unrelated drift\n", encoding="utf-8")
    with pytest.raises(TransactionRecoveryRequired):
        await gate.compensate("task-drift")
    assert target.read_text(encoding="utf-8") == "unrelated drift\n"
    assert gate.checkpoint_id("task-drift") is not None


@pytest.mark.asyncio
async def test_transaction_compensation_refuses_unrelated_drift_after_owned_write(
    tmp_path: Path,
):
    project = tmp_path / "project"
    project.mkdir()
    target = project / "state.txt"
    target.write_text("base\n", encoding="utf-8")
    workspace = WorkspaceSpec(
        id="repo",
        root=str(project),
        mutation_mode=MutationMode.SPECULATIVE,
    )
    shadow = ShadowEngine(
        roots_parent=str(tmp_path / "shadows"),
        state_root=str(tmp_path / "state"),
    )
    gate = RealityGate(
        shadow,
        checkpoint_manager=CheckpointManager(
            root=str(tmp_path / "checkpoints"),
        ),
    )
    request = CapabilityRequest(
        capability_id="fs",
        arguments={"operation": "write", "path": "state.txt", "content": "owned\n"},
        task_id="task-owned-drift",
        call_id="call-owned-drift",
    )
    route = await gate.route(
        request,
        workspace,
        {EffectClass.WRITE_LOCAL},
        FilesystemCapability().descriptor,
        tier="transactional",
    )
    result = await FilesystemCapability().invoke(
        request,
        context=InvocationContext(
            workspace=route.workspace,
            directives=DispatchDirectives(),
        ),
    )
    assert result.status.value == "ok"
    await gate.note_transaction_progress("task-owned-drift", str(project), mutation=True)
    (project / "unrelated.txt").write_text("external\n", encoding="utf-8")

    with pytest.raises(TransactionRecoveryRequired):
        await gate.compensate("task-owned-drift")
    assert target.read_text(encoding="utf-8") == "owned\n"
    assert (project / "unrelated.txt").read_text(encoding="utf-8") == "external\n"


@pytest.mark.asyncio
async def test_proven_transaction_stays_frozen_until_task_finalization(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "state.txt").write_text("base\n", encoding="utf-8")
    workspace = WorkspaceSpec(
        id="repo",
        root=str(project),
        mutation_mode=MutationMode.SPECULATIVE,
    )
    shadow = ShadowEngine(
        roots_parent=str(tmp_path / "shadows"),
        state_root=str(tmp_path / "state"),
    )
    gate = RealityGate(
        shadow,
        checkpoint_manager=CheckpointManager(
            root=str(tmp_path / "checkpoints"),
        ),
    )
    request = CapabilityRequest(
        capability_id="fs",
        arguments={"operation": "write", "path": "state.txt", "content": "owned\n"},
        task_id="task-proven",
        call_id="call-proven",
    )
    route = await gate.route(
        request,
        workspace,
        {EffectClass.WRITE_LOCAL},
        FilesystemCapability().descriptor,
        tier="transactional",
    )
    target = Path(route.workspace.root) / "state.txt"
    target.write_text("owned\n", encoding="utf-8")
    await gate.note_transaction_progress("task-proven", str(project), mutation=True)
    gate.mark_transaction_proven("task-proven")

    assert gate._transaction_records["task-proven"]["state"] == "COMMIT_PROVEN"
    with pytest.raises(PermissionError, match="pending durable task finalization"):
        await gate.route(
            CapabilityRequest(
                capability_id="fs",
                arguments={"operation": "read", "path": "state.txt"},
                task_id="task-proven",
                call_id="read-after-proof",
            ),
            workspace,
            {EffectClass.READ_LOCAL},
            FilesystemCapability().descriptor,
        )

    await gate.finalize_transaction("task-proven")
    assert gate.checkpoint_id("task-proven") is None


@pytest.mark.asyncio
async def test_transactional_verification_uses_cas_against_verified_revision(
    tmp_path: Path,
):
    project = tmp_path / "project"
    project.mkdir()
    target = project / "state.txt"
    target.write_text("base\n", encoding="utf-8")
    workspace = WorkspaceSpec(
        id="repo",
        root=str(project),
        mutation_mode=MutationMode.SPECULATIVE,
    )
    shadow = ShadowEngine(
        roots_parent=str(tmp_path / "shadows"),
        state_root=str(tmp_path / "state"),
    )
    registry = CapabilityRegistry()
    registry.register(FilesystemCapability())
    dispatcher = CapabilityDispatcher(registry, PolicyEngine(profile="offline"))
    shadow.bind(dispatcher)
    checkpoints = CheckpointManager(root=str(tmp_path / "checkpoints"))
    gate = RealityGate(shadow, checkpoint_manager=checkpoints)
    request = CapabilityRequest(
        capability_id="fs",
        arguments={"operation": "write", "path": "state.txt", "content": "owned\n"},
        task_id="task-transaction-cas",
        call_id="call-transaction-cas",
    )
    route = await gate.route(
        request,
        workspace,
        {EffectClass.WRITE_LOCAL},
        FilesystemCapability().descriptor,
        tier="transactional",
    )
    result = await FilesystemCapability().invoke(
        request,
        context=InvocationContext(workspace=route.workspace, directives=DispatchDirectives()),
    )
    assert result.status.value == "ok"
    await gate.note_transaction_progress(
        "task-transaction-cas",
        str(project),
        mutation=True,
    )
    owned = gate.transaction_fingerprint("task-transaction-cas")

    class _CASCheckpoints:
        def __init__(self):
            self.calls = 0

        async def fingerprint(self, root):
            self.calls += 1
            return owned if self.calls == 1 else "external-revision"

    # The real checkpoint worker established the owned revision above.  Use a
    # deterministic fingerprint double for the adversarial interleaving so
    # this test exercises the coordinator's CAS rather than host subprocess
    # scheduling.
    gate._checkpoints = _CASCheckpoints()

    verification_started = asyncio.Event()
    release_verification = asyncio.Event()

    class _Verifier:
        async def verify_against(self, task, criteria, workspace):
            verification_started.set()
            await release_verification.wait()
            return [{"id": criterion.id, "passed": True} for criterion in criteria]

    coordinator = RealityCoordinator(
        shadow_engine=shadow,
        reality_gate=gate,
        candidate_verifier=_Verifier(),
        completion_journal=CompletionJournal(tmp_path / "completion"),
    )
    task = TaskSpec(
        id="task-transaction-cas",
        objective="verify transaction",
        workspace=workspace,
        acceptance_criteria=(
            Criterion(
                id="state",
                description="state exists",
                verification=VerificationSpec(type=VerificationType.FILE, path="state.txt"),
            ),
        ),
    )
    decision = TerminationDecision(
        terminal=True,
        status=TaskStatus.COMPLETE,
        reason="done",
    )
    pending = asyncio.create_task(coordinator.prepare_completion(task, decision))
    await asyncio.wait_for(verification_started.wait(), timeout=5)
    target.write_text("external\n", encoding="utf-8")
    release_verification.set()
    outcome = await pending

    assert outcome.decision.status is TaskStatus.RECOVERY_REQUIRED
    assert gate._transaction_records[task.id]["state"] == "RECOVERY_REQUIRED"
    assert target.read_text(encoding="utf-8") == "external\n"


@pytest.mark.asyncio
async def test_completion_recovery_requires_fingerprint_and_final_identity(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()

    class _Shadow:
        _state_root = tmp_path

        async def workspace_fingerprint(self, root):
            raise OSError("workspace unavailable")

        def get_branch(self, branch_id):
            return None

    class _Tasks:
        async def get(self, task_id):
            return SimpleNamespace(
                id=task_id,
                workspace=WorkspaceSpec(id="repo", root=str(workspace)),
                metadata={"status": "RUNNING"},
            )

        async def finalize(self, *args, **kwargs):
            raise AssertionError("unavailable reality must not finalize")

    journal = CompletionJournal(tmp_path / "journal")
    journal.begin_verified(
        task_id="task-unavailable",
        branch_id="branch-unavailable",
        decision=SimpleNamespace(reason="done", summary="done", unresolved=()),
        certificate={},
    )
    journal.mark_commit_proven("task-unavailable", final_fingerprint="final")
    coordinator = RealityCoordinator(
        shadow_engine=_Shadow(),
        reality_gate=SimpleNamespace(),
        candidate_verifier=None,
        completion_journal=journal,
    )

    assert await coordinator.reconcile_startup(_Tasks()) == 0
    assert journal.record("task-unavailable")["state"] == "RECOVERY_REQUIRED"

    journal.begin_verified(
        task_id="task-missing-final",
        branch_id="branch-missing-final",
        decision=SimpleNamespace(reason="done", summary="done", unresolved=()),
        certificate={},
    )
    journal.mark_commit_proven("task-missing-final", final_fingerprint=None)
    assert await coordinator.reconcile_startup(_Tasks()) == 0
    assert journal.record("task-missing-final")["state"] == "RECOVERY_REQUIRED"


def test_completion_journal_survives_reload(tmp_path: Path):
    journal = CompletionJournal(tmp_path)
    journal.begin_verified(
        task_id="task-journal",
        branch_id="branch-journal",
        decision=SimpleNamespace(
            reason="done",
            summary="completed",
            unresolved=(),
        ),
        certificate={"candidate_fingerprint": "candidate"},
    )
    reloaded = CompletionJournal(tmp_path)
    assert reloaded.pending()[0]["state"] == "VERIFIED"
    reloaded.mark_commit_proven("task-journal", final_fingerprint="final")
    assert reloaded.pending()[0]["state"] == "COMMIT_PROVEN"
    reloaded.mark_finalized("task-journal")
    assert reloaded.pending() == ()


@pytest.mark.asyncio
async def test_rollback_refuses_to_overwrite_external_edit(tmp_path: Path):
    target = tmp_path / "state.txt"
    target.write_text("owned\n", encoding="utf-8")

    class _Store:
        def __init__(self):
            self.row = {
                "id": "mutation-rollback",
                "task_id": "task-rollback",
                "resource": str(target),
                "operation": "write",
                "status": "COMPLETED",
                "reversible": True,
                "after_state": __import__("hashlib").sha256(b"owned\n").hexdigest(),
                "before_state": None,
                "before_ref": None,
                "inverse": {"op": "delete", "target": str(target)},
            }
            self.rolled_back = False

        async def get(self, _mutation_id):
            return self.row

        async def record(self, *args, **kwargs):
            return "rollback-record"

        async def mark_rolled_back(self, _mutation_id):
            self.rolled_back = True

        async def mark_failed(self, *_args, **_kwargs):
            raise AssertionError("a conflict must not create a failed inverse")

        async def mark_reversible(self, *_args, **_kwargs):
            raise AssertionError("a conflict must not complete an inverse")

    store = _Store()
    target.write_text("external\n", encoding="utf-8")
    outcome = await RollbackExecutor(store).execute_inverse("mutation-rollback")

    assert outcome["status"] == "error"
    assert "rollback refused" in outcome["error"]
    assert store.rolled_back is False
    assert target.read_text(encoding="utf-8") == "external\n"


def test_verification_certificate_is_immutable_and_digest_bound():
    certificate = VerificationCertificate.issue(
        {
            "branch_id": "branch-proof",
            "criteria": [{"id": "parse", "passed": True}],
        }
    )
    original_hash = certificate["certificate_hash"]
    criteria = certificate["criteria"]
    criteria[0]["passed"] = False

    assert certificate["criteria"] == [{"id": "parse", "passed": True}]
    assert certificate["certificate_hash"] == original_hash
    with pytest.raises(TypeError):
        certificate["branch_id"] = "changed"  # type: ignore[index]


@pytest.mark.asyncio
async def test_startup_reconcile_releases_proven_transaction_checkpoint(tmp_path: Path):
    class _Shadow:
        _state_root = tmp_path

        def get_branch(self, branch_id):
            return None

        async def workspace_fingerprint(self, root):
            return "final"

    class _Gate:
        def __init__(self):
            self.finalized = []

        async def finalize_transaction(self, task_id):
            self.finalized.append(task_id)

    class _Tasks:
        def __init__(self):
            self.task = SimpleNamespace(
                id="task-reconcile",
                metadata={"status": "RUNNING"},
                workspace=SimpleNamespace(root=str(tmp_path)),
            )

        async def get(self, task_id):
            return self.task if task_id == self.task.id else None

        async def finalize(self, task_id, **kwargs):
            assert task_id == self.task.id
            assert kwargs["_allow_recovery_completion"] is True
            self.task.metadata["status"] = "COMPLETE"

    journal = CompletionJournal(tmp_path)
    journal.begin_verified(
        task_id="task-reconcile",
        branch_id="transaction:task-reconcile",
        decision=SimpleNamespace(reason="done", summary="done", unresolved=()),
        certificate={},
    )
    journal.mark_commit_proven("task-reconcile", final_fingerprint="final")
    gate = _Gate()
    coordinator = RealityCoordinator(
        shadow_engine=_Shadow(),
        reality_gate=gate,
        candidate_verifier=None,
        completion_journal=journal,
    )

    assert await coordinator.reconcile_startup(_Tasks()) == 1
    assert gate.finalized == ["task-reconcile"]
    assert journal.pending() == ()
