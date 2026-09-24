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
        input_schema={"type": "object", "additionalProperties": False},
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
