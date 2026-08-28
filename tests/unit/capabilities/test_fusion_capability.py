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
            id="branch-a",
            task_id="task-a",
            status="VERIFIED",
            proposal=[],
            verification=[],
            mutations=[],
            commit_plan=[],
            commit_outcome={},
            commit_state="NOT_STARTED",
            commit_started_at=None,
            commit_completed_at=None,
            checkpoint_id=None,
            error=None,
            policy_profile=None,
            created_at="2026-01-01T00:00:00+00:00",
        )

    def get_branch(self, branch_id):
        return self.branch if branch_id == self.branch.id else None


class _Orchestrator:
    def __init__(self):
        self.shadow = _Shadow()
        self.checkpoints = SimpleNamespace(
            inspect=self._inspect_checkpoint,
            release=self._release_checkpoint,
        )

    async def _inspect_checkpoint(self, checkpoint_id):
        return {
            "id": checkpoint_id,
            "task_id": "task-a",
            "metadata": {"type": "semantic_state_checkpoint"},
        }

    async def _release_checkpoint(self, checkpoint_id, *, owner):
        return owner == "task-a" and checkpoint_id == "ckpt-a"

    async def compare(self, **kwargs):
        return {
            "status": "COMPLETED",
            "candidate_count": len(kwargs["proposals"]),
            "reality_mutated": False,
        }


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
        capability_id="fusion",
        task_id=task_id,
        call_id=f"fusion-{operation}",
        arguments={"operation": operation, **arguments},
    )


async def test_failed_shadow_commit_compensates_completed_mutations(tmp_path):
    service = _Service()
    service.undone = []
    engine = ShadowEngine(state_root=str(tmp_path))
    engine.bind_service(service)
    successful = CapabilityResult(
        "call-a",
        "fs",
        CapabilityResultStatus.OK,
        metadata={"mutation": {"mutation_id": "mutation-a"}},
    )
    failed = CapabilityResult(
        "call-b",
        "fs",
        CapabilityResultStatus.FAILED,
        error="conflict",
    )

    outcome = await engine._rollback_partial_commit([successful, failed])

    assert outcome == {"rolled_back": ["mutation-a"], "errors": []}
    assert service.undone == ["mutation-a"]


async def test_fusion_branch_controls_are_task_owned():
    capability = FusionCapability(_Service())

    own = await capability.invoke(_request("task-a", "status", branch_id="branch-a"))
    foreign = await capability.invoke(_request("task-b", "status", branch_id="branch-a"))

    assert own.status is CapabilityResultStatus.OK
    assert foreign.status is CapabilityResultStatus.FAILED
    assert foreign.error == "branch not found"


async def test_fusion_compare_routes_bounded_candidates():
    capability = FusionCapability(_Service())
    result = await capability.invoke(
        _request(
            "task-a",
            "compare",
            proposals=[
                [{"capability_id": "fs", "arguments": {"operation": "read"}}],
                [{"capability_id": "fs", "arguments": {"operation": "list"}}],
            ],
        )
    )

    assert result.status is CapabilityResultStatus.OK
    assert '"candidate_count": 2' in result.output
    assert '"reality_mutated": false' in result.output


async def test_fusion_inspects_semantic_checkpoint_through_capability():
    capability = FusionCapability(_Service())
    result = await capability.invoke(
        _request(
            "task-a",
            "inspect_checkpoint",
            checkpoint_id="ckpt-a",
        )
    )

    assert result.status is CapabilityResultStatus.OK
    assert '"type": "semantic_state_checkpoint"' in result.output


async def test_fusion_releases_only_task_owned_checkpoint():
    capability = FusionCapability(_Service())

    own = await capability.invoke(
        _request(
            "task-a",
            "release_checkpoint",
            checkpoint_id="ckpt-a",
        )
    )
    foreign = await capability.invoke(
        _request(
            "task-b",
            "release_checkpoint",
            checkpoint_id="ckpt-a",
        )
    )

    assert own.status is CapabilityResultStatus.OK
    assert '"released": true' in own.output
    assert foreign.status is CapabilityResultStatus.FAILED
    assert foreign.error == "checkpoint not found"
