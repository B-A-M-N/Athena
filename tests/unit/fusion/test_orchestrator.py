"""Integration tests: shadow + worldstate + causal fork/checkpoint/synthesis
operating together through the FusionOrchestrator."""

from __future__ import annotations

import os

import pytest

from dataclasses import replace as dc_replace

from athena.fusion import FusionOrchestrator
from athena.protocol.tasks import AgentRequest
from athena.service.service import AthenaService
from athena.worldstate.core import ClaimStatus


@pytest.fixture
async def fused(tmp_path):
    svc = AthenaService.in_memory()
    await svc.start()
    ws = dc_replace(svc._default_workspace, root=str(tmp_path / "ws"))
    object.__setattr__(svc, "_default_workspace", ws)
    os.makedirs(ws.root, exist_ok=True)
    spec = await svc.submit(AgentRequest(prompt="fusion demo", workspace=ws), wait=True)
    try:
        yield svc, ws, spec.id
    finally:
        await svc.stop()


@pytest.mark.athena_scenario("CLAIM-002")
async def test_experiment_commit_binds_claim_and_invalidates(fused, tmp_path):
    """The full loop: propose -> shadow -> verify -> invariant gate ->
    commit -> claim bound to evidence -> later mutation makes it STALE."""
    svc, ws, task_id = fused
    fusion = FusionOrchestrator(svc)

    result = await fusion.run_experiment(
        task_id=task_id,
        proposal=[
            {
                "capability_id": "fs",
                "arguments": {
                    "operation": "write",
                    "path": "src/feature.py",
                    "content": "VALUE = 41\n",
                    "create_dirs": True,
                },
            }
        ],
        criteria_probes=[{"id": "file-exists", "command": f"test -f {ws.root}/src/feature.py"}],
        invariants=[],
        profile="autonomous",
        auto_fork_on_failure=False,
    )
    assert result.status == "COMMITTED", result.error
    assert result.claim_id is not None

    # Claim exists and is VERIFIED.
    wstate = svc.world_state(task_id)
    claim = wstate.claims.get(result.claim_id)
    assert claim is not None and claim.status == ClaimStatus.VERIFIED

    # Mutating the committed path invalidates the claim.
    flipped = wstate.claims.invalidate_for_paths(["src/feature.py"])
    assert any(c.id == result.claim_id for c in flipped)
    assert wstate.claims.get(result.claim_id).status == ClaimStatus.STALE


@pytest.mark.athena_scenario("TX-005")
async def test_failed_criteria_discards_and_auto_forks(fused):
    svc, ws, task_id = fused
    fusion = FusionOrchestrator(svc)

    # Criterion that cannot pass (file never written).
    result = await fusion.run_experiment(
        task_id=task_id,
        proposal=[
            {
                "capability_id": "fs",
                "arguments": {
                    "operation": "write",
                    "path": "other.py",
                    "content": "x=1\n",
                    "create_dirs": True,
                },
            }
        ],
        criteria_probes=[{"id": "impossible", "command": "test -f /nonexistent-target-file"}],
        profile="autonomous",
        auto_fork_on_failure=True,
    )
    assert result.status == "FAILED"
    # Reality untouched.
    assert not os.path.exists(os.path.join(ws.root, "other.py"))
    # Auto-fork created an alternate-approach branch.
    assert result.fork_id is not None and result.fork_id.startswith("task_")


async def test_invariant_violation_blocks_commit(fused):
    svc, ws, task_id = fused
    fusion = FusionOrchestrator(svc)

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


async def test_compare_verifies_alternatives_without_mutating_reality(fused):
    svc, ws, task_id = fused
    fusion = FusionOrchestrator(svc)

    outcome = await fusion.compare(
        task_id=task_id,
        proposals=[
            [
                {
                    "capability_id": "fs",
                    "arguments": {
                        "operation": "write",
                        "path": "candidate-a.py",
                        "content": "VALUE = 'a'\n",
                        "create_dirs": True,
                    },
                }
            ],
            [
                {
                    "capability_id": "fs",
                    "arguments": {
                        "operation": "write",
                        "path": "candidate-b.py",
                        "content": "VALUE = 'b'\n",
                        "create_dirs": True,
                    },
                }
            ],
        ],
        profile="autonomous",
    )

    assert outcome["status"] == "COMPLETED"
    assert outcome["candidate_count"] == 2
    assert outcome["verified_count"] == 2
    assert outcome["reality_mutated"] is False
    assert all(item["status"] == "DISCARDED" for item in outcome["candidates"])
    assert all(item["verified"] for item in outcome["candidates"])
    assert not os.path.exists(os.path.join(ws.root, "candidate-a.py"))
    assert not os.path.exists(os.path.join(ws.root, "candidate-b.py"))


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
