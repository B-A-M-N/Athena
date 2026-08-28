"""Shadow execution + world state (the fusion capability set)."""

from __future__ import annotations

import os

import pytest

from athena.service.service import AthenaService
from athena.protocol.tasks import AgentRequest
from dataclasses import replace as dc_replace


@pytest.fixture
async def service(tmp_path):
    svc = AthenaService.in_memory()
    await svc.start()
    ws = dc_replace(svc._default_workspace, root=str(tmp_path / "ws"))
    object.__setattr__(svc, "_default_workspace", ws)
    os.makedirs(ws.root, exist_ok=True)
    try:
        yield svc, ws
    finally:
        await svc.stop()


async def _real_task(service, ws):
    spec = await service.submit(AgentRequest(prompt="shadow demo", workspace=ws), wait=True)
    return spec.id


@pytest.mark.athena_scenario("TX-001")
async def test_shadow_commit_applies_proven_changes(service, tmp_path):
    svc, ws = service
    task_id = await _real_task(svc, ws)
    engine = svc.shadow_engine()

    branch = await engine.open_branch(
        task_id=task_id,
        base_workspace=ws,
        proposal=[
            {
                "capability_id": "fs",
                "arguments": {
                    "operation": "write",
                    "path": "lib.py",
                    "content": "x = 1\n",
                    "create_dirs": True,
                },
            }
        ],
    )

    # Reality untouched before commit.
    assert not os.path.exists(os.path.join(ws.root, "lib.py"))

    branch = await engine.execute_branch(branch, profile="autonomous")
    assert branch.error is None or branch.status == "EXECUTING"

    # Shadow has it; reality still doesn't.
    assert os.path.isfile(os.path.join(branch.shadow_workspace.root, "lib.py"))
    assert not os.path.exists(os.path.join(ws.root, "lib.py"))

    await engine.record_verification(branch, [{"id": "ac_1", "passed": True}])
    assert branch.verification_certificate["candidate_fingerprint"]
    assert branch.verification_certificate["environment_fingerprint"]
    assert branch.verification_certificate["criteria"] == [{"id": "ac_1", "passed": True}]
    outcome = await engine.commit(branch)
    assert outcome["status"] == "committed"
    assert "lib.py" in outcome["written"]
    assert os.path.isfile(os.path.join(ws.root, "lib.py"))


@pytest.mark.athena_scenario("TX-002")
async def test_shadow_discard_leaves_reality_untouched(service):
    svc, ws = service
    task_id = await _real_task(svc, ws)
    engine = svc.shadow_engine()

    branch = await engine.open_branch(
        task_id=task_id,
        base_workspace=ws,
        proposal=[
            {
                "capability_id": "fs",
                "arguments": {
                    "operation": "write",
                    "path": "junk.py",
                    "content": "bad\n",
                    "create_dirs": True,
                },
            }
        ],
    )
    branch = await engine.execute_branch(branch, profile="autonomous")
    outcome = await engine.discard(branch, reason="verification failed")
    assert outcome["status"] == "discarded"
    assert not os.path.exists(os.path.join(ws.root, "junk.py"))


async def test_commit_requires_verified_branch(service):
    svc, ws = service
    task_id = await _real_task(svc, ws)
    engine = svc.shadow_engine()
    branch = await engine.open_branch(task_id=task_id, base_workspace=ws, proposal=[])
    with pytest.raises(RuntimeError, match="cannot commit"):
        await engine.commit(branch)


@pytest.mark.athena_scenario("CLAIM-001")
async def test_claims_go_stale_after_dependent_mutation(service, tmp_path):
    svc, ws = service
    from athena.worldstate import ClaimStatus

    wstate = svc.world_state("task_ws_test")
    claim = wstate.claims.record(
        text="tests pass", evidence={"exit_code": 0}, depends_on_paths=("src/auth.py",)
    )
    assert claim.status == ClaimStatus.VERIFIED
    flipped = wstate.claims.invalidate_for_paths(["src/auth.py"])
    assert claim in flipped and claim.status == ClaimStatus.STALE


def svc_world(service):
    return service.world_state("task_ws_test")


@pytest.mark.athena_scenario("WORLD-001")
async def test_world_state_snapshot_shape(service):
    svc, ws = service
    task_id = await _real_task(svc, ws)
    snap = await svc.world_state(task_id).snapshot(workspace_root=ws.root)
    for key in ("task_id", "claims", "runtime_sessions", "mutations_since_verified_claim"):
        assert key in snap


async def test_invariant_envelope_detects_violation():
    from athena.worldstate import InvariantSet

    invariants = InvariantSet(task_id="t")
    state = {"tests_passing": True}

    async def probe():
        return state["tests_passing"]

    invariants.add("pytest always passes", probe)
    first = await invariants.check_all()
    assert first["ok"] is True

    state["tests_passing"] = False  # regression mid-task
    second = await invariants.check_all()
    assert second["ok"] is False
    assert len(second["violations"]) == 1
