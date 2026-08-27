"""Capability-boundary tests for fusion branch ownership."""

from __future__ import annotations

from types import SimpleNamespace

from athena.capabilities.fusion import FusionCapability
from athena.protocol.capabilities import (
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
)
from athena.shadow.engine import ShadowEngine


class _Shadow:
    def __init__(self):
        self.branch = SimpleNamespace(
            id="branch-a", task_id="task-a", status="VERIFIED",
            proposal=[], verification=[], mutations=[], commit_plan=[],
            commit_outcome={}, commit_state="NOT_STARTED",
            commit_started_at=None, commit_completed_at=None,
            checkpoint_id=None, error=None, policy_profile=None,
            created_at="2026-01-01T00:00:00+00:00",
        )

    def get_branch(self, branch_id):
        return self.branch if branch_id == self.branch.id else None


class _Orchestrator:
    def __init__(self):
        self.shadow = _Shadow()


class _Service:
    def __init__(self):
        self.orchestrator = _Orchestrator()

    def fusion_orchestrator(self):
        return self.orchestrator

    async def undo_mutation(self, mutation_id):
        self.undone.append(mutation_id)
        return {"status": "ok", "rollback_id": f"undo-{mutation_id}"}


def _request(task_id, operation, **arguments):
    return CapabilityRequest(
        capability_id="fusion", task_id=task_id, call_id=f"fusion-{operation}",
        arguments={"operation": operation, **arguments},
    )


async def test_failed_shadow_commit_compensates_completed_mutations(tmp_path):
    service = _Service()
    service.undone = []
    engine = ShadowEngine(state_root=str(tmp_path))
    engine.bind_service(service)
    successful = CapabilityResult(
        "call-a", "fs", CapabilityResultStatus.OK,
        metadata={"mutation": {"mutation_id": "mutation-a"}},
    )
    failed = CapabilityResult(
        "call-b", "fs", CapabilityResultStatus.FAILED, error="conflict",
    )

    outcome = await engine._rollback_partial_commit([successful, failed])

    assert outcome == {"rolled_back": ["mutation-a"], "errors": []}
    assert service.undone == ["mutation-a"]


async def test_fusion_branch_controls_are_task_owned():
    capability = FusionCapability(_Service())

    own = await capability.invoke(_request("task-a", "status", branch_id="branch-a"))
    foreign = await capability.invoke(
        _request("task-b", "status", branch_id="branch-a")
    )

    assert own.status is CapabilityResultStatus.OK
    assert foreign.status is CapabilityResultStatus.FAILED
    assert foreign.error == "branch not found"
