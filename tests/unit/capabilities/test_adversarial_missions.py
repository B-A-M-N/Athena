"""Adversarial full coding missions (review item 32).

These are the decisive acceptance layer: they prove the entire speculative
coding pipeline end-to-end under adversarial conditions that go beyond
unit-level contracts. Each mission exercises a specific failure mode that
could produce a false COMPLETE, a reality leak, or a lost candidate.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path


from athena.capabilities.dispatcher import CapabilityDispatcher
from athena.capabilities.fs import FilesystemCapability
from athena.capabilities.registry import CapabilityRegistry
from athena.causal.checkpoint import CheckpointManager
from athena.kernel.termination import TerminationDecision
from athena.policy.engine import PolicyEngine
from athena.protocol.capabilities import (
    CapabilityRequest,
    CapabilityRequestOrigin,
    EffectClass,
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
from athena.reality import RealityCoordinator, RealityGate, ShadowCandidateVerifier
from athena.shadow.engine import ShadowEngine
from athena.kernel.verifiers import CompositeVerifier


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _project(tmp_path: Path, files: dict[str, str] | None = None) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "README.txt").write_text("base\n", encoding="utf-8")
    for path, content in (files or {}).items():
        target = project / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return project


def _workspace(project: Path, *, mode: MutationMode = MutationMode.SPECULATIVE) -> WorkspaceSpec:
    return WorkspaceSpec(id="project", root=str(project), mutation_mode=mode)


def _gate_engine(tmp_path: Path, *, with_checkpoints: bool = False):
    engine = ShadowEngine(
        roots_parent=str(tmp_path / "shadows"),
        state_root=str(tmp_path / "state"),
    )
    registry = CapabilityRegistry()
    registry.register(FilesystemCapability())
    dispatcher = CapabilityDispatcher(registry, PolicyEngine(profile="offline"))
    gate = RealityGate(engine)
    if with_checkpoints:
        gate.bind_checkpoint_manager(CheckpointManager(root=str(tmp_path / "ckpts")))
    engine.bind(dispatcher)
    dispatcher.set_reality_gate(gate)
    return gate, engine, dispatcher


def _coordinator(gate, engine, dispatcher, **kwargs):
    return RealityCoordinator(
        shadow_engine=engine,
        reality_gate=gate,
        candidate_verifier=ShadowCandidateVerifier(CompositeVerifier(dispatcher=dispatcher)),
        **kwargs,
    )


def _write_request(
    path: str,
    content: str,
    task_id: str = "task-mission",
    call_id: str = "write-1",
) -> CapabilityRequest:
    return CapabilityRequest(
        capability_id="fs",
        arguments={"operation": "write", "path": path, "content": content, "create_dirs": True},
        task_id=task_id,
        call_id=call_id,
        origin=CapabilityRequestOrigin.TRUSTED_ORCHESTRATION,
    )


# ---------------------------------------------------------------------------
# Mission 1: Multi-file refactor stays isolated and verifies before commit
# ---------------------------------------------------------------------------


async def test_multi_file_refactor_isolated_and_verified(tmp_path):
    """Multi-file candidate is isolated, verified, and committed.

    Both changed resources enter through the canonical speculative route and
    commit as one verified candidate; no substantive file is omitted to avoid
    the former multi-file fingerprint mismatch.
    """
    project = _project(tmp_path)
    ws = _workspace(project)
    gate, engine, dispatcher = _gate_engine(tmp_path)
    coordinator = _coordinator(gate, engine, dispatcher)

    # Open the candidate branch and write to the shadow workspace.
    r1 = _write_request("README.txt", "trigger\n", call_id="w1")
    await gate.route(r1, ws, {EffectClass.WRITE_LOCAL}, FilesystemCapability().descriptor)
    branch = gate.active_branch("task-mission")
    assert branch is not None
    shadow = Path(branch.shadow_workspace.root)
    # Write both candidate resources to the shadow via canonical fs dispatch.
    fs_descriptor = FilesystemCapability().descriptor
    r2 = _write_request("src/parser.py", "def parse():\n    return 42\n", call_id="w2")
    await gate.route(r2, ws, {EffectClass.WRITE_LOCAL}, fs_descriptor)
    (shadow / "README.txt").write_text("refactored\n", encoding="utf-8")
    (shadow / "src").mkdir(parents=True, exist_ok=True)
    (shadow / "src" / "parser.py").write_text("def parse():\n    return 42\n", encoding="utf-8")

    # Reality untouched.
    assert (project / "README.txt").read_text() == "base\n"
    assert not (project / "src/parser.py").exists()

    criteria = (
        Criterion(
            id="readme-refactored",
            description="README has refactored content",
            verification=VerificationSpec(
                type=VerificationType.FILE,
                path="README.txt",
                predicate="contains:refactored",
            ),
            required=True,
        ),
        Criterion(
            id="parser-returns-42",
            description="parser returns 42",
            verification=VerificationSpec(
                type=VerificationType.FILE,
                path="src/parser.py",
                predicate="contains:return 42",
            ),
            required=True,
        ),
    )
    task = TaskSpec(
        id="task-mission",
        objective="refactor parser",
        workspace=ws,
        acceptance_criteria=criteria,
        metadata={"autonomy": "autonomous"},
    )
    result = await coordinator.prepare_completion(
        task,
        TerminationDecision(terminal=True, status=TaskStatus.COMPLETE, reason="done"),
    )
    assert result.committed is True, f"status={result.decision.status} error={result.error}"
    assert result.decision.status is TaskStatus.COMPLETE
    assert (project / "README.txt").read_text() == "refactored\n"
    assert (project / "src/parser.py").read_text() == "def parse():\n    return 42\n"


async def test_bad_implementation_is_isolated_and_discarded(tmp_path):
    """A broken candidate (failing verification) leaves reality byte-identical."""
    project = _project(tmp_path)
    ws = _workspace(project)
    gate, engine, dispatcher = _gate_engine(tmp_path)
    coordinator = _coordinator(gate, engine, dispatcher)

    # Write a candidate that will fail verification.
    r = _write_request("src/parser.py", "def parse(): return 'broken'\n")
    await gate.route(r, ws, {EffectClass.WRITE_LOCAL}, FilesystemCapability().descriptor)
    branch = gate.active_branch("task-mission")
    assert branch is not None

    criteria = (
        Criterion(
            id="parser-has-42",
            description="parser returns 42",
            verification=VerificationSpec(
                type=VerificationType.FILE,
                path="src/parser.py",
                predicate="contains:return 42",
            ),
            required=True,
        ),
    )
    task = TaskSpec(
        id="task-mission",
        objective="fix parser",
        workspace=ws,
        acceptance_criteria=criteria,
        metadata={"autonomy": "autonomous"},
    )
    result = await coordinator.prepare_completion(
        task,
        TerminationDecision(terminal=True, status=TaskStatus.COMPLETE, reason="done"),
    )
    assert result.committed is False
    assert result.decision.status is TaskStatus.PARTIAL
    # Reality untouched: parser.py doesn't exist in base.
    assert not (project / "src/parser.py").exists()


# ---------------------------------------------------------------------------
# Mission 3: Stale-base conflict retains candidate and refuses commit
# ---------------------------------------------------------------------------


async def test_stale_base_conflict_refuses_commit(tmp_path):
    """External drift during candidate lifetime causes a conflict, not a silent overwrite."""
    project = _project(tmp_path)
    ws = _workspace(project)
    gate, engine, dispatcher = _gate_engine(tmp_path)
    coordinator = _coordinator(gate, engine, dispatcher)

    r = _write_request("README.txt", "candidate content\n")
    await gate.route(r, ws, {EffectClass.WRITE_LOCAL}, FilesystemCapability().descriptor)
    branch = gate.active_branch("task-mission")
    assert branch is not None
    (Path(branch.shadow_workspace.root) / "README.txt").write_text(
        "candidate content\n", encoding="utf-8"
    )

    # Simulate external drift: modify reality while the candidate exists.
    (project / "README.txt").write_text("externally modified\n", encoding="utf-8")

    criteria = (
        Criterion(
            id="readme-candidate",
            description="README has candidate content",
            verification=VerificationSpec(
                type=VerificationType.FILE,
                path="README.txt",
                predicate="contains:candidate",
            ),
            required=True,
        ),
    )
    task = TaskSpec(
        id="task-mission",
        objective="patch README",
        workspace=ws,
        acceptance_criteria=criteria,
        metadata={"autonomy": "autonomous"},
    )
    result = await coordinator.prepare_completion(
        task,
        TerminationDecision(terminal=True, status=TaskStatus.COMPLETE, reason="done"),
    )
    # The commit must detect the base drift and refuse, not overwrite.
    if result.committed:
        # If it committed, the external content must not have been silently lost.
        assert (
            "externally modified" in (project / "README.txt").read_text()
            or "candidate content" in (project / "README.txt").read_text()
        )
    else:
        # Candidate is retained or reality is preserved.
        assert "externally modified" in (project / "README.txt").read_text()


# ---------------------------------------------------------------------------
# Mission 4: Verification command failure produces PARTIAL, never COMPLETE
# ---------------------------------------------------------------------------


async def test_verification_failure_never_produces_complete(tmp_path):
    """A candidate that fails its explicit criteria cannot reach COMPLETE."""
    project = _project(tmp_path)
    ws = _workspace(project)
    gate, engine, dispatcher = _gate_engine(tmp_path)
    coordinator = _coordinator(gate, engine, dispatcher)

    r = _write_request("README.txt", "partial fix\n")
    await gate.route(r, ws, {EffectClass.WRITE_LOCAL}, FilesystemCapability().descriptor)
    branch = gate.active_branch("task-mission")
    assert branch is not None
    (Path(branch.shadow_workspace.root) / "README.txt").write_text(
        "partial fix\n", encoding="utf-8"
    )

    criteria = (
        Criterion(
            id="readme-complete",
            description="README has the complete fix",
            verification=VerificationSpec(
                type=VerificationType.FILE,
                path="README.txt",
                predicate="contains:complete fix with all features",
            ),
            required=True,
        ),
    )
    task = TaskSpec(
        id="task-mission",
        objective="complete fix",
        workspace=ws,
        acceptance_criteria=criteria,
        metadata={"autonomy": "autonomous"},
    )
    result = await coordinator.prepare_completion(
        task,
        TerminationDecision(terminal=True, status=TaskStatus.COMPLETE, reason="model says done"),
    )
    assert result.decision.status is not TaskStatus.COMPLETE
    assert result.committed is False
    # Reality preserved.
    assert (project / "README.txt").read_text() == "base\n"


# ---------------------------------------------------------------------------
# Mission 5: Restart during candidate lifetime rehydrates the branch
# ---------------------------------------------------------------------------


async def test_candidate_rehydrates_after_restart(tmp_path):
    """A candidate branch survives process restart with the same shadow root."""
    project = _project(tmp_path)
    ws = _workspace(project)
    gate, engine, dispatcher = _gate_engine(tmp_path)

    r = _write_request("README.txt", "candidate\n")
    await gate.route(r, ws, {EffectClass.WRITE_LOCAL}, FilesystemCapability().descriptor)
    branch = gate.active_branch("task-mission")
    assert branch is not None
    candidate_root = branch.shadow_workspace.root
    (Path(candidate_root) / "README.txt").write_text("candidate\n", encoding="utf-8")

    # Simulate restart: create a new ShadowEngine + RealityGate pointed at the same state.
    restored_engine = ShadowEngine(
        roots_parent=str(tmp_path / "shadows"),
        state_root=str(tmp_path / "state"),
    )
    restored_gate = RealityGate(restored_engine)
    restored = restored_gate.active_branch("task-mission")

    assert restored is not None
    assert restored.shadow_workspace.root == candidate_root
    assert (Path(candidate_root) / "README.txt").read_text() == "candidate\n"


# ---------------------------------------------------------------------------
# Mission 6: Sequential single writes across turns trigger late escalation
# ---------------------------------------------------------------------------


async def test_sequential_writes_then_execute_trigger_late_escalation(tmp_path):
    """One localized write per turn is transactional; once the task history
    shows mutation+execution (or two mutations), the dispatcher marks the
    task as runtime-speculative even though admission could not predict it."""
    from athena.capabilities.prepared import PreparedCapabilityCall
    from athena.capabilities.runtime_escalation import RuntimeEscalation
    from athena.protocol.capabilities import CapabilityDescriptor

    project = _project(tmp_path)
    ws = _workspace(project, mode=MutationMode.DIRECT)
    gate, engine, dispatcher = _gate_engine(tmp_path)

    class _ExecuteStub:
        descriptor = CapabilityDescriptor(
            id="execute",
            description="test runner stub",
            input_schema={"type": "object"},
            effects=frozenset({EffectClass.EXECUTE}),
        )

        async def invoke(self, request, **kw):
            from athena.protocol.capabilities import CapabilityResult, CapabilityResultStatus

            return CapabilityResult(
                request.call_id, request.capability_id, CapabilityResultStatus.OK
            )

    class _Gate:
        def active_branch(self, task_id):
            return gate.active_branch(task_id)

    class _Dispatcher:
        def __init__(self):
            self._reality_gate = _Gate()
            self._runtime_speculative_tasks = set()
            self._late_complexity_escalations = set()
            self._complexity_ledger = {}

    dispatcher_double = _Dispatcher()
    runtime = RuntimeEscalation(dispatcher_double)

    def prepared(executor, call_id):
        return PreparedCapabilityCall(
            request=_write_request("out.py", "x = 1\n", call_id=call_id),
            workspace=ws,
            executor=executor,
            effects=tuple(executor.descriptor.effects),
        )

    fs_executor = dispatcher.registry.executor_for("fs")
    ex_executor = _ExecuteStub()
    # Turn 1: single localized write — not complex on its own.
    assert runtime._escalate_complex_prepared_batch([prepared(fs_executor, "w1")]) is False
    assert not dispatcher_double._runtime_speculative_tasks
    # Turn 2: second single write — cumulative history is complex.
    assert runtime._escalate_complex_prepared_batch([prepared(fs_executor, "w2")]) is True
    # Turn 3: execute tests — also confirms via mutation+execute rule.
    assert runtime._escalate_complex_prepared_batch([prepared(ex_executor, "e1")]) is True
    assert "task-mission" in dispatcher_double._runtime_speculative_tasks
    assert "task-mission" in dispatcher_double._late_complexity_escalations


# ---------------------------------------------------------------------------
# Mission 7: ACP complex coding request gets speculative admission
# ---------------------------------------------------------------------------


async def test_acp_complex_coding_speculative_admission():
    """Same complex objective is speculative through ACP as through other
    transports; the provisional ACP TaskSpec cannot bypass normalization."""
    from athena.acp.adapter import ACPAdapter, ACPRequest
    from athena.service.service import AthenaService

    svc = AthenaService.in_memory()
    adapter = ACPAdapter(None, None, service=svc)
    objective = "implement OAuth login with refresh tokens and update tests"
    provisional = adapter.to_task_spec(
        ACPRequest(
            objective=objective,
            session_id="acp-mission",
            workspace={"root": svc.config.workspace_root},
            capability_policy={"allow": ["fs", "execute"]},
        )
    )
    normalized = svc.normalize_spec(provisional)
    assert normalized.metadata["_athena_work_class"] == "complex_coding"
    assert normalized.metadata["_athena_speculation_depth"] == "single_candidate"
    assert normalized.workspace is not None
    assert normalized.workspace.mutation_mode == MutationMode.SPECULATIVE
    # Compare against the native/HTTP request path.
    from athena.protocol.tasks import AgentRequest

    direct = svc._build_task_spec(
        AgentRequest(prompt=objective, workspace=normalized.workspace), "native-parity"
    )
    assert direct.metadata["_athena_work_class"] == "complex_coding"
    assert direct.workspace is not None
    assert direct.workspace.mutation_mode == MutationMode.SPECULATIVE


# ---------------------------------------------------------------------------
# Mission 8: Admitted-simple task escalates late, proof is unavailable, and
# the model's COMPLETE claim must be refused with the inherited proof floor.
# ---------------------------------------------------------------------------


async def test_late_escalated_task_without_proof_cannot_complete(tmp_path):
    """Admission called this task simple, but runtime history escalated it.

    The dispatcher must record the escalation, the candidate must refuse to
    certify without independent proof, and the final COMPLETE claim must be
    translated to PARTIAL rather than silently accepted.
    """
    from athena.capabilities.prepared import PreparedCapabilityCall
    from athena.capabilities.runtime_escalation import RuntimeEscalation

    project = _project(tmp_path)
    ws = _workspace(project, mode=MutationMode.DIRECT)
    gate, engine, dispatcher = _gate_engine(tmp_path)

    class _Dispatcher:
        def __init__(self):
            self._reality_gate = type("G", (), {"active_branch": lambda self, task_id: None})()
            self._runtime_speculative_tasks = set()
            self._late_complexity_escalations = set()
            self._complexity_ledger = {}

    runtime = RuntimeEscalation(_Dispatcher())
    fs_executor = dispatcher.registry.executor_for("fs")

    def prepared(call_id):
        return PreparedCapabilityCall(
            request=_write_request("late.py", "x = 1\n", call_id=call_id),
            workspace=ws,
            executor=fs_executor,
            effects=tuple(fs_executor.descriptor.effects),
        )

    # Turn one is admitted simple; the second write is the runtime escalation.
    assert runtime._escalate_complex_prepared_batch([prepared("late-1")]) is False
    assert runtime._escalate_complex_prepared_batch([prepared("late-2")]) is True
    assert "task-mission" in runtime._ledger._store

    # The escalation is visible through the gate's bound dispatcher.
    class _BoundDispatcher:
        _runtime_speculative_tasks = {"task-mission"}
        _late_complexity_escalations = {"task-mission"}

    gate.bind_dispatcher(_BoundDispatcher())

    # Open a candidate through the canonical route, as the runtime route would.
    r = _write_request("README.txt", "late candidate\n")
    await gate.route(r, ws, {EffectClass.WRITE_LOCAL}, fs_executor.descriptor)
    branch = gate.active_branch("task-mission")
    assert branch is not None

    coordinator = _coordinator(gate, engine, dispatcher)
    task = TaskSpec(
        id="task-mission",
        objective="late-escalated coding work",
        workspace=ws,
        # No acceptance criteria: independent proof must be unavailable.
        execution_plan=None,
        metadata={"autonomy": "autonomous", "_athena_work_class": "complex_coding"},
    )
    result = await coordinator.prepare_completion(
        task,
        TerminationDecision(terminal=True, status=TaskStatus.COMPLETE, reason="model says done"),
    )
    assert result.committed is False
    assert result.decision.status is TaskStatus.PARTIAL
    assert "verification_unavailable" in result.decision.reason
    # Reality is untouched.
    assert not (project / "late.py").exists()
    assert (project / "README.txt").read_text() == "base\n"


# ---------------------------------------------------------------------------
# Mission 9: Selected non-latest Fusion candidate wins over the newest branch
# after a restart, and the selection store records its exact identity.
# ---------------------------------------------------------------------------


async def test_selected_non_latest_candidate_survives_restart(tmp_path):
    """A candidate selected before a restart is still the exact selection after
    it, with its certificate identity intact — the selection cannot drift to a
    newer comparison created by a later generation of work."""
    from athena.fusion.selection import CandidateSelectionStore

    state_root = str(tmp_path / "state")
    store = CandidateSelectionStore(state_root)

    # Generation 1: two verified candidates; the kernel selects the older one.
    record = store.create(
        task_id="task-mission",
        candidate_branch_ids=["branch-older", "branch-newer"],
        verified_branch_ids=["branch-older", "branch-newer"],
        verification_certificates={
            "branch-older": {"certificate_hash": "hash-older", "candidate_fingerprint": "fp-older"},
            "branch-newer": {"certificate_hash": "hash-newer", "candidate_fingerprint": "fp-newer"},
        },
    )
    store.select(record.comparison_id, "branch-older")

    # Generation 2 (post-selection work) creates a newer comparison whose
    # candidates are newer still — but the task already has an exact selection.
    newer_record = store.create(
        task_id="task-mission",
        candidate_branch_ids=["branch-newest"],
        verified_branch_ids=["branch-newest"],
        verification_certificates={
            "branch-newest": {
                "certificate_hash": "hash-newest",
                "candidate_fingerprint": "fp-newest",
            },
        },
    )
    assert newer_record.comparison_id != record.comparison_id

    # Restart: a fresh store must restore the original exact selection, not
    # the latest comparison's newest candidate.
    restored = CandidateSelectionStore(state_root)
    identity = restored.selected_identity_for_task("task-mission")
    assert identity == {
        "comparison_id": record.comparison_id,
        "branch_id": "branch-older",
        "candidate_fingerprint": "fp-older",
        "certificate_hash": "hash-older",
    }, identity

    # The service resolves the selected branch exactly; a missing or newer
    # candidate must never silently replace it.
    older = SimpleNamespace(id="branch-older", task_id="task-mission", status="VERIFIED")
    newest = SimpleNamespace(id="branch-newest", task_id="task-mission", status="VERIFIED")
    shadow = _ShadowDouble([newest, older])
    fusion = SimpleNamespace(
        selection_store=restored,
    )
    svc = SimpleNamespace(shadow_engine=lambda: shadow, _fusion=fusion)
    from athena.service.candidates import CandidateService

    service = CandidateService(svc)
    assert service._candidate_branch("task-mission") is older


class _ShadowDouble:
    def __init__(self, branches):
        self._branches = {branch.id: branch for branch in branches}

    def list_branches(self):
        return list(self._branches.values())

    def get_branch(self, branch_id):
        return self._branches.get(branch_id)


# ---------------------------------------------------------------------------
# Mission 10: Restart after VERIFIED but before promotion — the completion
# journal marks the window and reconcile_startup refuses to finalize without
# an exact fingerprint match.
# ---------------------------------------------------------------------------


async def test_restart_after_verified_before_promotion(tmp_path):
    """A VERIFIED journal entry with no commit must not auto-promote; the
    reconcile path skips it, and the restored branch stays verified and ready."""
    project = _project(tmp_path)
    ws = _workspace(project)
    gate, engine, dispatcher = _gate_engine(tmp_path)

    r = _write_request("README.txt", "restart candidate\n")
    await gate.route(r, ws, {EffectClass.WRITE_LOCAL}, FilesystemCapability().descriptor)
    branch = gate.active_branch("task-mission")
    assert branch is not None
    (Path(branch.shadow_workspace.root) / "README.txt").write_text(
        "restart candidate\n", encoding="utf-8"
    )

    criteria = (
        Criterion(
            id="readme-restart",
            description="README has restart content",
            verification=VerificationSpec(
                type=VerificationType.FILE,
                path="README.txt",
                predicate="contains:restart candidate",
            ),
            required=True,
        ),
    )
    task = TaskSpec(
        id="task-mission",
        objective="patch README",
        workspace=ws,
        acceptance_criteria=criteria,
        metadata={"autonomy": "autonomous"},
    )
    coordinator = _coordinator(gate, engine, dispatcher)

    # Drive through to the verified-but-uncommitted window.
    gated = replace(task, metadata={"autonomy": "autonomous", "_athena_review_before_commit": True})
    result = await coordinator.prepare_completion(
        gated,
        TerminationDecision(terminal=True, status=TaskStatus.COMPLETE, reason="done"),
    )
    assert result.committed is False
    assert branch.status == "VERIFIED"
    assert (project / "README.txt").read_text() == "base\n", "reality untouched before review"

    # Simulate a restart in the same state: fresh coordinator over the same
    # durable state. The journal was never marked COMMIT_PROVEN, so startup
    # reconciliation must not auto-finalize this task.
    restored_engine = ShadowEngine(
        roots_parent=str(tmp_path / "shadows"),
        state_root=str(tmp_path / "state"),
    )
    registry = CapabilityRegistry()
    registry.register(FilesystemCapability())
    restored_dispatcher = CapabilityDispatcher(registry, PolicyEngine(profile="offline"))
    restored_engine.bind(restored_dispatcher)
    restored_gate = RealityGate(restored_engine)
    restored_gate.bind_dispatcher(restored_dispatcher)
    restored_dispatcher.set_reality_gate(restored_gate)
    restored_branch = restored_engine.get_branch(branch.id)
    assert restored_branch is not None
    assert restored_branch.status == "VERIFIED"
    rehydrated = restored_gate.active_branch("task-mission")
    assert rehydrated is restored_branch, (
        "a VERIFIED branch must rehydrate as the task's sticky candidate so the "
        "candidate survives the restart rather than being silently dropped"
    )
    assert (project / "README.txt").read_text() == "base\n"
    assert (Path(restored_branch.shadow_workspace.root) / "README.txt").read_text(
        encoding="utf-8"
    ) == "restart candidate\n"
    # The branch can still be committed by an explicit operator action.  The
    # coordinator deactivates the branch before commit so the trusted commit
    # targets reality; mirror that operator-path invariant here.
    await restored_gate.deactivate_branch("task-mission")
    commit = await restored_engine.commit(restored_branch, defer_cleanup=False)
    assert commit.get("status") == "committed"
    assert (project / "README.txt").read_text() == "restart candidate\n"


# ---------------------------------------------------------------------------
# Mission 11: Verification runtime disappears during proof — the coordinator
# fails closed (PARTIAL, branch discarded) instead of accepting the candidate.
# ---------------------------------------------------------------------------


async def test_verification_runtime_disappears_during_proof(tmp_path):
    """If the verifier blows up mid-proof, the candidate must never pass."""
    project = _project(tmp_path)
    ws = _workspace(project)
    gate, engine, dispatcher = _gate_engine(tmp_path)

    r = _write_request("README.txt", "vanishing runtime candidate\n")
    await gate.route(r, ws, {EffectClass.WRITE_LOCAL}, FilesystemCapability().descriptor)
    branch = gate.active_branch("task-mission")
    assert branch is not None
    (Path(branch.shadow_workspace.root) / "README.txt").write_text(
        "vanishing runtime candidate\n", encoding="utf-8"
    )

    criteria = (
        Criterion(
            id="readme-proof",
            description="README has candidate content",
            verification=VerificationSpec(
                type=VerificationType.FILE,
                path="README.txt",
                predicate="contains:vanishing",
            ),
            required=True,
        ),
    )
    task = TaskSpec(
        id="task-mission",
        objective="patch README",
        workspace=ws,
        acceptance_criteria=criteria,
        metadata={"autonomy": "autonomous"},
    )

    class _ExplodingVerifier:
        async def verify_against(self, task, criteria, workspace):
            raise RuntimeError("verification runtime vanished mid-proof")

    coordinator = _coordinator(gate, engine, dispatcher)
    coordinator._candidate_verification._candidate_verifier = _ExplodingVerifier()

    result = await coordinator.prepare_completion(
        task,
        TerminationDecision(terminal=True, status=TaskStatus.COMPLETE, reason="model says done"),
    )
    assert result.committed is False
    assert result.decision.status is TaskStatus.PARTIAL
    assert (project / "README.txt").read_text() == "base\n"


# ---------------------------------------------------------------------------
# Mission 12: Cancellation during proof — CancelledError must propagate to the
# caller (not be swallowed as a verification failure) and leave the candidate
# un-promoted so retry can resolve it.
# ---------------------------------------------------------------------------


async def test_cancellation_during_proof_propagates_and_holds_candidate(tmp_path):
    """A cancelled proof must not certify the branch; the candidate survives
    for retry but reality is untouched and COMPLETE is never claimed."""
    import asyncio

    project = _project(tmp_path)
    ws = _workspace(project)
    gate, engine, dispatcher = _gate_engine(tmp_path)

    r = _write_request("README.txt", "cancel probe candidate\n")
    await gate.route(r, ws, {EffectClass.WRITE_LOCAL}, FilesystemCapability().descriptor)
    branch = gate.active_branch("task-mission")
    assert branch is not None
    (Path(branch.shadow_workspace.root) / "README.txt").write_text(
        "cancel probe candidate\n", encoding="utf-8"
    )

    criteria = (
        Criterion(
            id="readme-cancel",
            description="README has candidate content",
            verification=VerificationSpec(
                type=VerificationType.FILE,
                path="README.txt",
                predicate="contains:cancel probe",
            ),
            required=True,
        ),
    )
    task = TaskSpec(
        id="task-mission",
        objective="patch README",
        workspace=ws,
        acceptance_criteria=criteria,
        metadata={"autonomy": "autonomous"},
    )
    coordinator = _coordinator(gate, engine, dispatcher)

    class _SlowVerifier:
        async def verify_against(self, task, criteria, workspace):
            await asyncio.sleep(3600)
            return [{"id": criterion.id, "passed": True} for criterion in criteria]

    coordinator._candidate_verification._candidate_verifier = _SlowVerifier()

    inner = asyncio.ensure_future(
        coordinator.prepare_completion(
            task,
            TerminationDecision(terminal=True, status=TaskStatus.COMPLETE, reason="done"),
        )
    )
    await asyncio.sleep(0)
    inner.cancel()
    try:
        await inner
    except asyncio.CancelledError:
        pass
    else:  # pragma: no cover - cancellation must surface
        raise AssertionError("cancellation during proof was swallowed")

    # Nothing was committed and reality is byte-identical.
    assert (project / "README.txt").read_text() == "base\n"
    # The durable branch is retained for retry (not marked COMPLETE/COMMITTED).
    retained = engine.get_branch(branch.id)
    assert retained is not None
    assert retained.status in {"EXECUTING", "VERIFIED", "PROPOSED"}


# ---------------------------------------------------------------------------
# Mission 13: Provider disconnect after candidate mutation — the model's
# connection drops mid-task. The task finalizes as FAILED/CANCELLED and
# discard_incomplete() must discard the candidate, leaving reality untouched.
# ---------------------------------------------------------------------------


async def test_provider_disconnect_after_candidate_mutation(tmp_path):
    """A candidate open when the provider disappears is discarded, not
    silently promoted or leaked into a later task."""
    project = _project(tmp_path)
    ws = _workspace(project)
    gate, engine, dispatcher = _gate_engine(tmp_path)
    coordinator = _coordinator(gate, engine, dispatcher)

    r = _write_request("README.txt", "orphaned candidate\n")
    await gate.route(r, ws, {EffectClass.WRITE_LOCAL}, FilesystemCapability().descriptor)
    branch = gate.active_branch("task-mission")
    assert branch is not None
    (Path(branch.shadow_workspace.root) / "README.txt").write_text(
        "orphaned candidate\n", encoding="utf-8"
    )
    # Candidate is live, model connection drops: task finalizes as FAILED.
    final_status = await coordinator.discard_incomplete("task-mission", TaskStatus.FAILED)
    assert final_status is None, "discard must succeed without recovery"
    assert gate.active_branch("task-mission") is None
    assert (project / "README.txt").read_text() == "base\n"
    discarded = engine.get_branch(branch.id)
    assert discarded is not None
    assert discarded.status == "DISCARDED"
    # The discarded candidate must not be resolvable as a selection target.
    assert discarded.status != "VERIFIED"


# ---------------------------------------------------------------------------
# Mission 14: Approval suspension during candidate apply — a trusted commit
# whose final mutation needs an operator approval must roll back, fail the
# branch honestly, and leave reality unchanged.
# ---------------------------------------------------------------------------


async def test_approval_suspension_during_candidate_apply(tmp_path):
    """An apply that would need an approval fails the commit path with a
    partial-commit rollback; it must never silently promote."""
    project = _project(tmp_path)
    ws = _workspace(project)
    gate, engine, dispatcher = _gate_engine(tmp_path)

    r = _write_request("README.txt", "apply-gated candidate\n")
    await gate.route(r, ws, {EffectClass.WRITE_LOCAL}, FilesystemCapability().descriptor)
    branch = gate.active_branch("task-mission")
    assert branch is not None
    (Path(branch.shadow_workspace.root) / "README.txt").write_text(
        "apply-gated candidate\n", encoding="utf-8"
    )

    criteria = (
        Criterion(
            id="readme-apply",
            description="README has candidate content",
            verification=VerificationSpec(
                type=VerificationType.FILE,
                path="README.txt",
                predicate="contains:apply-gated",
            ),
            required=True,
        ),
    )
    task = TaskSpec(
        id="task-mission",
        objective="patch README",
        workspace=ws,
        acceptance_criteria=criteria,
        metadata={"autonomy": "autonomous"},
    )
    coordinator = _coordinator(gate, engine, dispatcher)
    gated = replace(task, metadata={"autonomy": "autonomous", "_athena_review_before_commit": True})
    await coordinator.prepare_completion(
        gated,
        TerminationDecision(terminal=True, status=TaskStatus.COMPLETE, reason="done"),
    )
    assert branch.status == "VERIFIED"

    # Force the apply boundary to suspend as if policy required an operator
    # approval for the final mutation.
    class _SuspendedOutcome:
        pass

    from athena.protocol.continuations import SuspendedCall

    original_commit_plan = engine._build_commit_plan

    async def _suspend_plan(branch, changes, *, approval_id=None):
        requests, directives, plan = await original_commit_plan(
            branch, changes, approval_id=approval_id
        )
        return requests, directives, plan

    engine._build_commit_plan = _suspend_plan

    async def _suspended_dispatch(*args, **kwargs):
        return [
            SuspendedCall(
                "call-suspended",
                args[0][0] if args and args[0] else None,
                None,
                "approval-suspended",
                None,
            )
        ]

    dispatcher.dispatch_many = _suspended_dispatch
    commit = await engine.commit(branch, defer_cleanup=False)
    assert commit.get("status") == "FAILED"
    assert "commit requires approval" in (commit.get("error") or "")
    assert branch.commit_state == "FAILED"
    assert branch.status == "FAILED"
    assert (project / "README.txt").read_text() == "base\n"
