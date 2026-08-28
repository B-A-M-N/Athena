from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from athena.kernel.verifiers import CompositeVerifier
from athena.protocol.capabilities import (
    CapabilityResult,
    CapabilityResultStatus,
)
from athena.protocol.tasks import (
    Criterion,
    TaskSpec,
    VerificationSpec,
    VerificationType,
    WorkspaceSpec,
)
from athena.reality import ExecutionDisposition, RealityClassificationInput, RealityClassifier
from athena.reality.completion import CompletionJournal
from athena.reality.coordinator import RealityCoordinator


def test_reality_classifier_is_deterministic_and_explainable():
    facts = RealityClassificationInput(
        capability_id="fs",
        operation="write",
        effects=frozenset({"WRITE_LOCAL"}),
        origin="native",
        persistent_mutation=True,
        reversible=True,
        target_resources=("src/a.py",),
        checkpoint_available=True,
    )
    classifier = RealityClassifier()

    first = classifier.classify(facts)
    second = classifier.classify(facts)

    assert first == second
    assert first.disposition is ExecutionDisposition.TRANSACTIONAL
    assert first.workspace_lock_scope == ("src/a.py",)
    assert first.reasons
    assert first.to_record()["disposition"] == "transactional"


@pytest.mark.asyncio
async def test_verification_probe_mutation_invalidates_evidence(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("pass\n", encoding="utf-8")
    workspace = WorkspaceSpec(id="repo", root=str(source))

    class _MutatingDispatcher:
        async def dispatch(self, request, *, workspace, **kwargs):
            (Path(workspace.root) / "main.py").write_text("mutated\n", encoding="utf-8")
            return CapabilityResult(
                request.call_id,
                request.capability_id,
                CapabilityResultStatus.OK,
            )

    verifier = CompositeVerifier(dispatcher=_MutatingDispatcher())
    criterion = Criterion(
        id="mutating-probe",
        description="probe",
        verification=VerificationSpec(
            type=VerificationType.COMMAND,
            command="probe",
        ),
    )
    result = await verifier.verify(
        TaskSpec(id="task", objective="verify", workspace=workspace),
        (criterion,),
    )

    assert result == [False]
    assert (source / "main.py").read_text(encoding="utf-8") == "pass\n"


@pytest.mark.asyncio
async def test_restart_completion_refuses_final_workspace_drift(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "state.txt").write_text("drifted\n", encoding="utf-8")
    journal = CompletionJournal(tmp_path / "state")
    journal.begin_verified(
        task_id="task-final",
        branch_id="transaction:task-final",
        decision=SimpleNamespace(reason="done", summary="done", unresolved=()),
        certificate={},
    )
    journal.mark_commit_proven("task-final", final_fingerprint="owned")

    class _Shadow:
        async def workspace_fingerprint(self, root):
            return "drifted"

        def get_branch(self, branch_id):
            return None

    class _Gate:
        _checkpoints = None

        async def finalize_transaction(self, task_id):
            raise AssertionError("drifted completion must not finalize")

    class _Tasks:
        async def get(self, task_id):
            return SimpleNamespace(
                id=task_id,
                workspace=WorkspaceSpec(id="repo", root=str(workspace)),
                metadata={"status": "RUNNING"},
            )

        async def finalize(self, *args, **kwargs):
            raise AssertionError("drifted completion must remain recoverable")

    coordinator = RealityCoordinator(
        shadow_engine=_Shadow(),
        reality_gate=_Gate(),
        candidate_verifier=None,
        completion_journal=journal,
    )

    assert await coordinator.reconcile_startup(_Tasks()) == 0
    assert journal.record("task-final")["state"] == "RECOVERY_REQUIRED"
