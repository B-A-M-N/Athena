"""Shadow commit/discard gating, conflicts, cleanup, canonical commit path.

Pins contracts of ``athena.shadow.engine`` beyond TX-001..005:

* commit is refused for every non-VERIFIED branch status (PROPOSED, FAILED,
  DISCARDED) and reality stays untouched in each case;
* an ``ask``-profiled operation inside the shadow fails the branch honestly
  instead of auto-approving (shadow is filesystem isolation, not execution
  isolation) and no grant is ever installed;
* commit detects reality drift after verification and returns CONFLICT
  without overwriting the concurrent edit;
* a verified branch whose commit would need approval suspends instead of
  granting, and the execute-time profile is remembered for commit;
* discard and successful commit both remove the shadow workspace, and a
  committed branch's writes land in the durable mutation ledger;
* deletions in the shadow propagate to reality through the canonical fs
  delete capability with a durable mutation record.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from dataclasses import replace as dc_replace

from athena.protocol.tasks import AgentRequest
from athena.service.service import AthenaService
from athena.shadow.engine import BranchStatus


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


async def _real_task(service, ws) -> str:
    svc = service
    spec = await svc.submit(AgentRequest(prompt="commit-discard", workspace=ws), wait=True)
    return spec.id


def _write_prop(path: str, content: str) -> dict:
    return {
        "capability_id": "fs",
        "arguments": {"operation": "write", "path": path, "content": content, "create_dirs": True},
    }


async def test_commit_refuses_branches_not_in_verified_status(service):
    svc, ws = service
    task_id = await _real_task(svc, ws)
    engine = svc.shadow_engine()

    branch = await engine.open_branch(
        task_id=task_id, base_workspace=ws, proposal=[_write_prop("junk.py", "bad\n")]
    )
    assert branch.status == BranchStatus.PROPOSED
    with pytest.raises(RuntimeError, match=r"cannot commit branch .* PROPOSED"):
        await engine.commit(branch)

    branch = await engine.execute_branch(branch, profile="autonomous")
    await engine.record_verification(branch, [{"id": "ac", "passed": False}])
    assert branch.status == BranchStatus.FAILED
    with pytest.raises(RuntimeError, match=r"cannot commit branch .* FAILED"):
        await engine.commit(branch)

    await engine.discard(branch, reason="not proven")
    with pytest.raises(RuntimeError, match=r"cannot commit branch .* DISCARDED"):
        await engine.commit(branch)

    assert not os.path.exists(os.path.join(ws.root, "junk.py"))


async def test_shadow_op_requiring_approval_fails_branch_without_auto_grant(service):
    svc, ws = service
    task_id = await _real_task(svc, ws)
    engine = svc.shadow_engine()

    branch = await engine.open_branch(
        task_id=task_id, base_workspace=ws, proposal=[_write_prop("needs_approval.py", "x\n")]
    )
    # Supervised profile: an fs write is an ASK decision.
    branch = await engine.execute_branch(branch, profile="supervised")

    assert branch.status == BranchStatus.FAILED
    assert "requires approval" in branch.error
    assert "do not auto-approve" in branch.error
    assert len(branch.rejected_requests) == 1

    # No grant was installed anywhere.
    assert svc._policy.approvals.list_active() == []
    # Nothing was written: neither reality nor the shadow clone.
    assert not os.path.exists(os.path.join(ws.root, "needs_approval.py"))
    assert not os.path.exists(os.path.join(branch.shadow_workspace.root, "needs_approval.py"))


async def test_commit_returns_conflict_when_reality_drifted(service):
    svc, ws = service
    task_id = await _real_task(svc, ws)
    engine = svc.shadow_engine()

    branch = await engine.open_branch(
        task_id=task_id, base_workspace=ws, proposal=[_write_prop("drift.txt", "shadow\n")]
    )
    branch = await engine.execute_branch(branch, profile="autonomous")
    await engine.record_verification(branch, [{"id": "ac", "passed": True}])

    # Reality creates the same file after the branch was opened.
    (Path(ws.root) / "drift.txt").write_text("changed-by-someone-else\n")

    outcome = await engine.commit(branch)
    assert outcome["status"] == "CONFLICT"
    assert outcome["conflicts"][0]["resource"] == "drift.txt"
    assert outcome["conflicts"][0]["reason"] == "created_elsewhere"
    # A conflict is retained as an explicit recoverable candidate rather than
    # being collapsed into an ordinary execution failure.
    assert branch.status == BranchStatus.CONFLICTED
    assert os.path.isdir(branch.shadow_workspace.root)
    assert branch.error and "CONFLICT" in branch.error
    # The concurrent edit is preserved, not overwritten by the branch.
    assert (Path(ws.root) / "drift.txt").read_text() == "changed-by-someone-else\n"


async def test_commit_retains_and_reports_unsupported_symlink(service):
    svc, ws = service
    task_id = await _real_task(svc, ws)
    engine = svc.shadow_engine()
    branch = await engine.open_branch(
        task_id=task_id,
        base_workspace=ws,
        proposal=[_write_prop("link.txt", "unused\n")],
    )
    link = Path(branch.shadow_workspace.root) / "link.txt"
    link.symlink_to("README.txt")
    await engine.record_verification(branch, [{"id": "ac", "passed": True}])

    outcome = await engine.commit(branch)

    assert outcome["status"] == "UNSUPPORTED_RESOURCE"
    assert outcome["resources"] == [{"resource": "link.txt", "kind": "symlink"}]
    assert branch.status == BranchStatus.FAILED
    assert branch.commit_state == "UNSUPPORTED_RESOURCE"
    assert link.is_symlink()
    assert not (Path(ws.root) / "link.txt").exists()


async def test_commit_through_ask_profile_suspends_instead_of_auto_approving(service):
    svc, ws = service
    task_id = await _real_task(svc, ws)
    engine = svc.shadow_engine()

    branch = await engine.open_branch(
        task_id=task_id, base_workspace=ws, proposal=[_write_prop("gated.py", "proven\n")]
    )
    branch = await engine.execute_branch(branch, profile="autonomous")
    # The execute-time profile is remembered so commit evaluates the same
    # policy the branch was proven under.
    assert branch.policy_profile == "autonomous"

    await engine.record_verification(branch, [{"id": "ac", "passed": True}])
    branch.policy_profile = "supervised"

    outcome = await engine.commit(branch)
    assert outcome["status"] == "STALE_CERTIFICATE"
    assert "verification certificate stale" in outcome["error"]
    assert not os.path.exists(os.path.join(ws.root, "gated.py"))
    # The candidate remains available for re-verification under the new
    # policy; stale proof never reaches the mutation dispatcher.
    assert svc._policy.approvals.list_active() == []
    rows = await svc._store_approvals.list_for_task(task_id)
    assert rows == []

    await engine.record_verification(branch, [{"id": "ac", "passed": True}])
    outcome = await engine.commit(branch)
    assert outcome["status"] == "FAILED"
    assert outcome["error"] == "commit not applied: commit requires approval"
    assert not os.path.exists(os.path.join(ws.root, "gated.py"))
    # Re-verification under supervised policy reaches the approval boundary;
    # it is still never auto-granted.
    rows = await svc._store_approvals.list_for_task(task_id)
    assert all(r["status"] == "PENDING" for r in rows)


async def test_discard_and_commit_remove_shadow_workspace(service):
    svc, ws = service
    task_id = await _real_task(svc, ws)
    engine = svc.shadow_engine()

    discarded = await engine.open_branch(
        task_id=task_id, base_workspace=ws, proposal=[_write_prop("thrown.py", "away\n")]
    )
    discarded = await engine.execute_branch(discarded, profile="autonomous")
    shadow_root = discarded.shadow_workspace.root
    assert os.path.isdir(shadow_root)
    outcome = await engine.discard(discarded, reason="not needed")
    assert outcome["status"] == "discarded"
    assert not os.path.exists(shadow_root)

    committed = await engine.open_branch(
        task_id=task_id, base_workspace=ws, proposal=[_write_prop("kept.py", "value\n")]
    )
    committed = await engine.execute_branch(committed, profile="autonomous")
    await engine.record_verification(committed, [{"id": "ac", "passed": True}])
    shadow_root = committed.shadow_workspace.root
    outcome = await engine.commit(committed)
    assert outcome["status"] == "committed"
    assert not os.path.exists(shadow_root)
    # The commit went through the canonical mutation path: durable ledger
    # rows exist for the applied write.
    assert outcome["mutation_results"], "commit must record mutations"
    assert all(m["mutation_id"] for m in outcome["mutation_results"])
    rows = await svc._store_mutations.list_for_task(task_id)
    assert any(r["operation"] == "write" and r["resource"].endswith("kept.py") for r in rows)


async def test_operator_can_review_and_apply_retained_candidate(service):
    svc, ws = service
    task_id = await _real_task(svc, ws)
    engine = svc.shadow_engine()

    branch = await engine.open_branch(
        task_id=task_id,
        base_workspace=ws,
        proposal=[_write_prop("reviewed.py", "candidate\n")],
    )
    branch = await engine.execute_branch(branch, profile="autonomous")
    await engine.record_verification(branch, [{"id": "ac", "passed": True}])
    svc._reality_gate.activate_branch(branch)

    review = await svc.operator_candidate(task_id)
    assert review is not None
    assert review["status"] == BranchStatus.VERIFIED
    assert review["changed_resources"][0]["path"] == "reviewed.py"
    assert not (Path(ws.root) / "reviewed.py").exists()

    outcome = await svc.apply_candidate(task_id)
    assert outcome["status"] == "committed"
    assert (Path(ws.root) / "reviewed.py").read_text() == "candidate\n"
    assert await svc.operator_candidate(task_id) is None


async def test_operator_cannot_apply_failed_candidate(service):
    svc, ws = service
    task_id = await _real_task(svc, ws)
    engine = svc.shadow_engine()

    branch = await engine.open_branch(
        task_id=task_id,
        base_workspace=ws,
        proposal=[_write_prop("rejected.py", "candidate\n")],
    )
    branch = await engine.execute_branch(branch, profile="autonomous")
    await engine.record_verification(branch, [{"id": "ac", "passed": False}])

    refused = await svc.apply_candidate(task_id)
    assert refused["status"] == "missing"
    assert not (Path(ws.root) / "rejected.py").exists()


async def test_operator_can_discard_unapplied_candidate(service):
    svc, ws = service
    task_id = await _real_task(svc, ws)
    engine = svc.shadow_engine()

    branch = await engine.open_branch(
        task_id=task_id,
        base_workspace=ws,
        proposal=[_write_prop("discarded.py", "candidate\n")],
    )
    review = await svc.operator_candidate(task_id)
    assert review is not None
    assert review["status"] == BranchStatus.PROPOSED

    discarded = await svc.discard_candidate(task_id)
    assert discarded["status"] == "discarded"
    assert not Path(branch.shadow_workspace.root).exists()
    assert await svc.operator_candidate(task_id) is None


async def test_commit_deletion_requires_approval_and_never_lands_unapproved(service):
    """Deletions are ASK under every autonomy profile: a shadow branch that
    removed a file can never silently delete it from reality at commit.
    Effect truthfulness -- no irreversible effect hides behind a
    "transactional" commit claim."""
    svc, ws = service
    task_id = await _real_task(svc, ws)
    engine = svc.shadow_engine()

    # A file that exists in reality BEFORE the branch opens (so the captured
    # base manifest includes it).
    (Path(ws.root) / "stale.txt").write_text("to be removed\n")

    branch = await engine.open_branch(task_id=task_id, base_workspace=ws, proposal=[])
    branch = await engine.execute_branch(branch, profile="autonomous")
    await engine.record_verification(branch, [{"id": "ac", "passed": True}])

    # The branch's work removed the file inside the shadow clone.
    os.unlink(os.path.join(branch.shadow_workspace.root, "stale.txt"))

    outcome = await engine.commit(branch)
    assert outcome["status"] == "STALE_CERTIFICATE"
    assert "verification certificate stale" in outcome["error"]
    # The irreversible effect never landed.
    assert (Path(ws.root) / "stale.txt").read_text() == "to be removed\n"
    # No delete mutation was recorded against reality.
    rows = await svc._store_mutations.list_for_task(task_id)
    assert not any(r["operation"] == "delete" for r in rows)
    # The stale certificate is rejected before an unverified delete can reach
    # policy, so no approval is created.
    assert svc._policy.approvals.list_active() == []
    pend = await svc._store_approvals.list_pending(task_id)
    assert not any(r["capability_id"] == "fs" for r in pend)
