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
    spec = await svc.submit(AgentRequest(prompt="fusion demo", workspace=ws),
                            wait=True)
    try:
        yield svc, ws, spec.id
    finally:
        await svc.stop()


async def test_experiment_commit_binds_claim_and_invalidates(fused, tmp_path):
    """The full loop: propose -> shadow -> verify -> invariant gate ->
    commit -> claim bound to evidence -> later mutation makes it STALE."""
    svc, ws, task_id = fused
    fusion = FusionOrchestrator(svc)

    result = await fusion.run_experiment(
        task_id=task_id,
        proposal=[{"capability_id": "fs", "arguments": {
            "operation": "write", "path": "src/feature.py",
            "content": "VALUE = 41\n", "create_dirs": True}}],
        criteria_probes=[
            {"id": "file-exists",
             "command": f"test -f {ws.root}/src/feature.py"}],
        invariants=[],
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


async def test_failed_criteria_discards_and_auto_forks(fused):
    svc, ws, task_id = fused
    fusion = FusionOrchestrator(svc)

    # Criterion that cannot pass (file never written).
    result = await fusion.run_experiment(
        task_id=task_id,
        proposal=[{"capability_id": "fs", "arguments": {
            "operation": "write", "path": "other.py",
            "content": "x=1\n", "create_dirs": True}}],
        criteria_probes=[
            {"id": "impossible", "command": "test -f /nonexistent-target-file"}],
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

    async def broken():
        return False  # required invariant violated

    result = await fusion.run_experiment(
        task_id=task_id,
        proposal=[{"capability_id": "fs", "arguments": {
            "operation": "write", "path": "v.py", "content": "1\n",
            "create_dirs": True}}],
        invariants=[{"description": "system must remain healthy",
                     "probe": broken}],
        auto_fork_on_failure=False,
    )
    assert result.status == "FAILED"
    assert "invariant violation" in (result.error or "")
    assert not os.path.exists(os.path.join(ws.root, "v.py"))


async def test_fork_from_event_with_checkpoint(fused):
    svc, ws, task_id = fused
    fusion = FusionOrchestrator(svc)
    timeline = await fusion.forker.timeline(task_id)
    assert timeline, "parent has events to fork from"
    seq = timeline[-1]["sequence"]

    outcome = await fusion.fork_from_event(
        task_id=task_id, after_event_sequence=seq, capture_checkpoint=True)
    fork_id = outcome["fork_id"]
    assert fork_id != task_id
    row = await svc._store_tasks.get(fork_id)
    meta = row.get("metadata") or {}
    assert meta.get("fork_of") == task_id
    assert meta.get("fork_after_event") == seq
    assert outcome.get("checkpoint_id")


async def test_synthesized_capability_with_shadow_provenance(fused):
    """Synthesis integrated with the orchestrator's proof chain."""
    svc, ws, task_id = fused
    fusion = FusionOrchestrator(svc)
    outcome = await fusion.synthesize_from_branch(
        svc._registry,
        name="double_value",
        description="doubles an integer input",
        code="def run(args):\n    return {'doubled': args['n'] * 2}\n",
        input_schema={"type": "object", "properties": {"n": {"type": "integer"}},
                      "required": ["n"]},
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
