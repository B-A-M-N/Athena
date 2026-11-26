"""Integration tests: shadow + worldstate + causal fork/checkpoint/synthesis
operating together through the FusionOrchestrator."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from dataclasses import replace as dc_replace

from athena.fusion import FusionOrchestrator
from athena.protocol.tasks import AgentRequest, AutonomyLevel
from athena.service.service import AthenaService


@pytest.fixture
async def fused(tmp_path):
    svc = AthenaService.in_memory()
    await svc.start()
    ws = dc_replace(svc._default_workspace, root=str(tmp_path / "ws"))
    object.__setattr__(svc, "_default_workspace", ws)
    os.makedirs(ws.root, exist_ok=True)
    spec = await svc.submit(
        AgentRequest(prompt="fusion demo", workspace=ws, autonomy=AutonomyLevel.AUTONOMOUS),
        wait=True,
    )
    try:
        yield svc, ws, spec.id
    finally:
        await svc.stop()


@pytest.mark.athena_scenario("CLAIM-002")
async def test_verified_fusion_candidate_awaits_reality_promotion(fused):
    """Fusion proves the candidate but never promotes it into reality."""
    svc, ws, task_id = fused
    # Supply explicit file criteria through the task contract; canonical
    # verifier uses these as the proof floor.
    criteria = [
        {
            "id": "candidate-exists",
            "description": "candidate file contains expected value",
            "verification": {
                "type": "file",
                "path": "candidate.py",
                "predicate": "contains:VALUE = 41",
            },
            "required": True,
        }
    ]
    await svc._store_tasks._db.execute(
        "UPDATE tasks SET acceptance_criteria = ? WHERE id = ?",
        (json.dumps(criteria), task_id),
    )
    fusion = FusionOrchestrator(svc)

    result = await fusion.run_experiment(
        task_id=task_id,
        proposal=[
            {
                "capability_id": "fs",
                "arguments": {
                    "operation": "write",
                    "path": "candidate.py",
                    "content": "VALUE = 41\n",
                    "create_dirs": True,
                },
            }
        ],
        invariants=[],
        profile="autonomous",
        auto_fork_on_failure=False,
    )
    assert result.status == "CANDIDATE_READY", result.error
    assert result.verified is True
    assert result.claim_id is None
    assert not os.path.exists(os.path.join(ws.root, "candidate.py"))


@pytest.mark.athena_scenario("C-03")
async def test_composed_fusion_candidate_reaches_reality_commit_seam(fused):
    """The real Fusion, Shadow, verifier, and Reality path commits once."""
    svc, ws, task_id = fused
    criteria = [
        {
            "id": "candidate-proof",
            "description": "candidate contains the value",
            "verification": {
                "type": "file",
                "path": "candidate.py",
                "predicate": "contains:VALUE = 41",
            },
            "required": True,
        }
    ]
    await svc._store_tasks._db.execute(
        "UPDATE tasks SET acceptance_criteria = ? WHERE id = ?",
        (json.dumps(criteria), task_id),
    )
    fusion = FusionOrchestrator(svc)
    result = await fusion.run_experiment(
        task_id=task_id,
        proposal=[
            {
                "capability_id": "fs",
                "arguments": {
                    "operation": "write",
                    "path": "candidate.py",
                    "content": "VALUE = 41\n",
                    "create_dirs": True,
                },
            }
        ],
        profile="autonomous",
        auto_fork_on_failure=False,
    )
    assert result.status == "CANDIDATE_READY", result.error
    branch = fusion.shadow.get_branch(result.branch_id)
    assert branch is not None
    task = await svc._task_manager.get(task_id)
    svc._reality_gate.activate_branch(branch)
    completion = await svc._reality_coordinator.prepare_completion(
        task,
        __import__(
            "athena.kernel.termination", fromlist=["TerminationDecision"]
        ).TerminationDecision(
            terminal=True,
            status=__import__("athena.protocol.tasks", fromlist=["TaskStatus"]).TaskStatus.COMPLETE,
            reason="candidate accepted",
        ),
    )
    assert completion.committed is True
    assert (Path(ws.root) / "candidate.py").read_text(encoding="utf-8") == "VALUE = 41\n"


async def test_m03_branch_synthesis_is_admitted_task_locally(fused):
    svc, ws, task_id = fused
    criteria = [
        {
            "id": "m03-source",
            "description": "source branch proof",
            "verification": {"type": "file", "path": "source.txt", "predicate": "contains:ready"},
            "required": True,
        }
    ]
    await svc._store_tasks._db.execute(
        "UPDATE tasks SET acceptance_criteria = ? WHERE id = ?",
        (json.dumps(criteria), task_id),
    )
    fusion = FusionOrchestrator(svc)
    experiment = await fusion.run_experiment(
        task_id=task_id,
        proposal=[
            {
                "capability_id": "fs",
                "arguments": {
                    "operation": "write",
                    "path": "source.txt",
                    "content": "ready\n",
                    "create_dirs": True,
                },
            }
        ],
        profile="autonomous",
        auto_fork_on_failure=False,
    )
    assert experiment.status == "CANDIDATE_READY", experiment.error
    outcome = await fusion.synthesize_from_branch(
        svc._registry,
        name="branch_transform",
        description="Transforms a source value inside the branch.",
        code="def run(args):\n    return {'value': args['value'].upper()}\n",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        effects={"READ_LOCAL"},
        task_id=task_id,
        validation_cases=[{"args": {"value": "ready"}, "expect_output": {"value": "READY"}}],
        branch_id=experiment.branch_id,
        workspace=fusion.shadow.get_branch(experiment.branch_id).shadow_workspace,
    )
    assert outcome["admitted"] is True
    assert outcome["branch_id"] == experiment.branch_id
    assert svc._fabric.has(outcome["capability_id"], task_id=task_id)


async def test_invariant_violation_blocks_commit_after_canonical_proof(fused):
    svc, ws, task_id = fused
    fusion = FusionOrchestrator(svc)
    criteria = [
        {
            "id": "v-file",
            "description": "candidate exists",
            "verification": {"type": "file", "path": "v.py"},
            "required": True,
        }
    ]
    await svc._store_tasks._db.execute(
        "UPDATE tasks SET acceptance_criteria = ? WHERE id = ?",
        (json.dumps(criteria), task_id),
    )
    result = await fusion.run_experiment(
        task_id=task_id,
        proposal=[
            {
                "capability_id": "fs",
                "arguments": {
                    "operation": "write",
                    "path": "v.py",
                    "content": "1\n",
                    "create_dirs": True,
                },
            }
        ],
        invariants=[{"description": "always fails", "command": "test -d /nonexistent-dir-xyz"}],
        profile="autonomous",
        auto_fork_on_failure=False,
    )
    assert result.status == "FAILED"
    assert "invariant violation" in (result.error or "")
    assert not os.path.exists(os.path.join(ws.root, "v.py"))


async def test_model_probe_cannot_replace_canonical_proof(fused):
    svc, ws, task_id = fused
    fusion = FusionOrchestrator(svc)

    with pytest.raises(TypeError, match="criteria_probes"):  # model arg removed
        await fusion.run_experiment(
            task_id=task_id,
            proposal=[
                {
                    "capability_id": "fs",
                    "arguments": {
                        "operation": "write",
                        "path": "model-only.py",
                        "content": "x = 1\n",
                        "create_dirs": True,
                    },
                }
            ],
            criteria_probes=[{"id": "model-true", "command": "true"}],
            profile="autonomous",
            auto_fork_on_failure=False,
        )
    assert not os.path.exists(os.path.join(ws.root, "model-only.py"))


async def test_fork_from_event_with_checkpoint(fused):
    svc, ws, task_id = fused
    fusion = FusionOrchestrator(svc)
    timeline = await fusion.forker.timeline(task_id)
    assert timeline, "parent has events to fork from"
    seq = timeline[-1]["sequence"]

    outcome = await fusion.fork_from_event(
        task_id=task_id, after_event_sequence=seq, capture_checkpoint=True
    )
    fork_id = outcome["fork_id"]
    assert fork_id != task_id
    row = await svc._store_tasks.get(fork_id)
    meta = row.get("metadata") or {}
    assert meta.get("fork_of") == task_id
    assert meta.get("fork_after_event") == seq
    assert outcome.get("checkpoint_id")


async def test_fusion_checkpoint_carries_semantic_state(fused):
    svc, ws, task_id = fused
    fusion = FusionOrchestrator(svc)

    manifest = await fusion.capture_checkpoint(
        task_id=task_id,
        workspace_root=ws.root,
        label="semantic integration",
    )
    metadata = manifest["metadata"]

    assert metadata["type"] == "semantic_state_checkpoint"
    assert metadata["version"] == 1
    state = metadata["state"]
    assert state["task"]["id"] == task_id
    assert "world_state" in state
    assert "attached_context" in state
    assert "runtime_sessions" in state
    assert "affordances" in state
    assert "shadow_branches" in state


async def test_synthesized_capability_with_shadow_provenance(fused):
    """Synthesis integrated with the orchestrator's proof chain."""
    svc, ws, task_id = fused
    fusion = FusionOrchestrator(svc)
    outcome = await fusion.synthesize_from_branch(
        svc._registry,
        name="double_value",
        description="doubles an integer input",
        code="def run(args):\n    return {'doubled': args['n'] * 2}\n",
        input_schema={
            "type": "object",
            "properties": {"n": {"type": "integer"}},
            "required": ["n"],
        },
        effects={"READ_LOCAL"},
        task_id=task_id,
        validation_cases=[
            {"args": {"n": 21}, "expect_output_contains": "42"},
            {"args": {"n": 5}, "expect_output_contains": "10"},
        ],
    )
    assert outcome["admitted"] is True
    assert outcome["validation"]["all_passed"] is True
    assert outcome["skill_candidate_proposed"] is False  # uses < 2 so far


async def test_synthesized_capability_binds_validation_to_exact_branch(fused):
    svc, ws, task_id = fused
    criteria = [
        {
            "id": "branch-file",
            "description": "candidate exists in the branch",
            "verification": {"type": "file", "path": "branch.txt"},
            "required": True,
        }
    ]
    await svc._store_tasks._db.execute(
        "UPDATE tasks SET acceptance_criteria = ? WHERE id = ?",
        (json.dumps(criteria), task_id),
    )
    fusion = FusionOrchestrator(svc)
    experiment = await fusion.run_experiment(
        task_id=task_id,
        proposal=[
            {
                "capability_id": "fs",
                "arguments": {
                    "operation": "write",
                    "path": "branch.txt",
                    "content": "branch\n",
                    "create_dirs": True,
                },
            }
        ],
        profile="autonomous",
        auto_fork_on_failure=False,
    )
    assert experiment.status == "CANDIDATE_READY", experiment.error
    branch = fusion.shadow.get_branch(experiment.branch_id)
    assert branch is not None
    before = await fusion.shadow.workspace_fingerprint(branch.shadow_workspace.root)
    outcome = await fusion.synthesize_from_branch(
        svc._registry,
        name="branch_reader",
        description="reads branch proof",
        code="def run(args):\n    return {'ok': True}\n",
        input_schema={
            "type": "object",
            "properties": {"variant": {"type": "string"}},
            "additionalProperties": False,
        },
        effects={"READ_LOCAL"},
        task_id=task_id,
        validation_cases=[{"args": {}, "expect_output": {"ok": True}}],
        branch_id=branch.id,
        workspace=branch.shadow_workspace,
    )
    assert outcome["branch_id"] == branch.id
    assert outcome["workspace_fingerprint"] == before
    assert outcome["verification_environment"] is None
    assert outcome["admitted"] is True


async def test_fusion_orchestrator_accepts_explicit_ports(tmp_path):
    """Fusion can be constructed from explicit ports without a service locator."""
    from athena.causal.checkpoint import CheckpointManager
    from athena.fusion.orchestrator import FusionOrchestrator
    from athena.shadow.engine import ShadowEngine

    shadow = ShadowEngine(
        roots_parent=str(tmp_path / "shadows"),
        state_root=str(tmp_path / "state"),
    )
    checkpoints = CheckpointManager(root=str(tmp_path / "ckpts"))

    fusion = FusionOrchestrator(
        ports={
            "shadow": shadow,
            "checkpoints": checkpoints,
            "store_tasks": None,
            "default_workspace": None,
            "world_state_factory": None,
            "synthesis_ref": None,
        }
    )
    assert fusion.service is None
    assert fusion.shadow is shadow
    assert fusion.checkpoints is checkpoints


async def test_composed_generated_capability_speculative_reality_and_provenance(fused):
    """Qualify generated capability, speculation, reality, and provenance as one flow."""
    from athena.kernel.termination import TerminationDecision
    from athena.protocol.capabilities import CapabilityRequest, CapabilityRequestOrigin
    from athena.protocol.tasks import TaskStatus

    svc, ws, task_id = fused
    criteria = [
        {
            "id": "composed-source",
            "description": "candidate source is present",
            "verification": {"type": "file", "path": "source.txt"},
            "required": True,
        },
    ]
    await svc._store_tasks._db.execute(
        "UPDATE tasks SET acceptance_criteria = ? WHERE id = ?",
        (json.dumps(criteria), task_id),
    )
    fusion = FusionOrchestrator(svc)
    experiment = await fusion.run_experiment(
        task_id=task_id,
        proposal=[
            {
                "capability_id": "fs",
                "arguments": {
                    "operation": "write",
                    "path": "source.txt",
                    "content": "ready\n",
                    "create_dirs": True,
                },
            }
        ],
        profile="autonomous",
        auto_fork_on_failure=False,
    )
    assert experiment.status == "CANDIDATE_READY", experiment.error
    branch = fusion.shadow.get_branch(experiment.branch_id)
    assert branch is not None

    generated = await fusion.synthesize_from_branch(
        svc._registry,
        name="branch_reader",
        description="reads the branch source through canonical fs",
        code=(
            "def run(args):\n"
            "    return {'value': athena.call('fs', {'operation': 'read', 'path': 'source.txt'})}\n"
        ),
        input_schema={
            "type": "object",
            "properties": {"variant": {"type": "string"}},
            "additionalProperties": False,
        },
        effects={"READ_LOCAL"},
        task_id=task_id,
        validation_cases=[
            {"args": {"variant": "one"}, "expect_output_contains": "ready"},
            {"args": {"variant": "two"}, "expect_output_contains": "ready"},
        ],
        branch_id=branch.id,
        workspace=branch.shadow_workspace,
    )
    assert generated["admitted"] is True
    generated_id = generated["capability_id"]
    call_ids = []
    for index, (session_id, variant) in enumerate(
        (
            ("composed-session-one", "one"),
            ("composed-session-two", "two"),
            ("composed-session-two", "three"),
        ),
        start=1,
    ):
        call_id = f"composed-generated-invocation-{index}"
        call_ids.append(call_id)
        execution = await svc._dispatcher.dispatch(
            CapabilityRequest(
                capability_id=generated_id,
                task_id=task_id,
                session_id=session_id,
                call_id=call_id,
                origin=CapabilityRequestOrigin.USER_DIRECT,
                arguments={"variant": variant},
            ),
            workspace=branch.shadow_workspace,
            profile="autonomous",
        )
        assert execution.status.value == "ok", execution.error
        assert "ready" in json.loads(execution.output)["value"]
    for call_id in call_ids:
        await svc._synthesis.observe_event(
            __import__("athena.protocol.events", fromlist=["make_event"]).make_event(
                "CapabilityCompleted",
                {"call_id": call_id, "capability_id": generated_id},
                task_id=task_id,
            )
        )
        await svc._synthesis.observe_event(
            __import__("athena.protocol.events", fromlist=["make_event"]).make_event(
                "VerificationCompleted", {"passed": True}, task_id=task_id
            )
        )
    skill_candidate = svc._synthesis.to_skill_candidate(generated_id)
    assert skill_candidate is not None
    generated_identity = skill_candidate.draft.metadata["athena"]["generated_capability"]
    assert generated_identity["branch_id"] == branch.id
    assert generated_identity["workspace_fingerprint"] == await fusion.shadow.workspace_fingerprint(
        branch.shadow_workspace.root
    )
    assert generated_identity["revision"] == 1

    svc._reality_gate.activate_branch(branch)
    task = await svc._task_manager.get(task_id)
    completion = await svc._reality_coordinator.prepare_completion(
        task,
        TerminationDecision(terminal=True, status=TaskStatus.COMPLETE, reason="composed proof"),
    )
    assert completion.committed is True
    assert Path(ws.root, "source.txt").read_text(encoding="utf-8") == "ready\n"
    assert generated_identity["branch_id"] == experiment.branch_id
    assert generated_identity["workspace_fingerprint"]


async def test_composed_branch_synthesis_rejects_cross_branch_workspace(fused):
    svc, ws, task_id = fused
    fusion = FusionOrchestrator(svc)
    criteria = [
        {
            "id": "branch-source",
            "description": "candidate",
            "verification": {"type": "file", "path": "source.txt"},
            "required": True,
        }
    ]
    await svc._store_tasks._db.execute(
        "UPDATE tasks SET acceptance_criteria = ? WHERE id = ?",
        (json.dumps(criteria), task_id),
    )

    async def verified_branch(path: str, content: str) -> str:
        result = await fusion.run_experiment(
            task_id=task_id,
            proposal=[
                {
                    "capability_id": "fs",
                    "arguments": {
                        "operation": "write",
                        "path": path,
                        "content": content,
                        "create_dirs": True,
                    },
                }
            ],
            profile="autonomous",
            auto_fork_on_failure=False,
        )
        assert result.status == "CANDIDATE_READY", result.error
        return result.branch_id

    first_id = await verified_branch("source.txt", "first\n")
    second_id = await verified_branch("source.txt", "second\n")
    first = fusion.shadow.get_branch(first_id)
    second = fusion.shadow.get_branch(second_id)
    assert first is not None and second is not None
    with pytest.raises(ValueError, match="workspace must be the selected branch workspace"):
        await fusion.synthesize_from_branch(
            svc._registry,
            name="cross_branch_reader",
            description="must never bind evidence across branches",
            code="def run(args):\n    return {'ok': True}\n",
            input_schema={"type": "object", "additionalProperties": False},
            effects={"READ_LOCAL"},
            task_id=task_id,
            validation_cases=[{"args": {}, "expect_output": {"ok": True}}],
            branch_id=first.id,
            workspace=second.shadow_workspace,
        )
    assert not os.path.exists(os.path.join(ws.root, "cross_branch_reader.py"))


async def test_composed_generation_cancellation_keeps_branch_and_reality_clean(fused, monkeypatch):
    import asyncio

    svc, ws, task_id = fused
    fusion = FusionOrchestrator(svc)
    criteria = [
        {
            "id": "branch-source",
            "description": "candidate",
            "verification": {"type": "file", "path": "cancel.txt"},
            "required": True,
        }
    ]
    await svc._store_tasks._db.execute(
        "UPDATE tasks SET acceptance_criteria = ? WHERE id = ?",
        (json.dumps(criteria), task_id),
    )
    experiment = await fusion.run_experiment(
        task_id=task_id,
        proposal=[
            {
                "capability_id": "fs",
                "arguments": {
                    "operation": "write",
                    "path": "cancel.txt",
                    "content": "candidate\n",
                    "create_dirs": True,
                },
            }
        ],
        profile="autonomous",
        auto_fork_on_failure=False,
    )
    assert experiment.status == "CANDIDATE_READY", experiment.error
    branch = fusion.shadow.get_branch(experiment.branch_id)
    assert branch is not None
    original_validate = svc._synthesis.validate

    async def slow_validate(*args, **kwargs):
        await asyncio.sleep(3600)
        return await original_validate(*args, **kwargs)

    monkeypatch.setattr(svc._synthesis, "validate", slow_validate)
    pending = asyncio.create_task(
        fusion.synthesize_from_branch(
            svc._registry,
            name="cancelled_generator",
            description="cancellation boundary",
            code="def run(args):\n    return {'ok': True}\n",
            input_schema={"type": "object", "additionalProperties": False},
            effects={"READ_LOCAL"},
            task_id=task_id,
            validation_cases=[{"args": {}}],
            branch_id=branch.id,
            workspace=branch.shadow_workspace,
        )
    )
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not os.path.exists(os.path.join(ws.root, "cancelled_generator.py"))
    assert fusion.shadow.get_branch(branch.id) is not None
