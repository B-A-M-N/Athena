"""Child-task isolation invariants (§70): a child's capability policy and
budget are scoped to a strict subset of the parent's, and a child can never
exceed the parent's privileges.
"""

from __future__ import annotations

from decimal import Decimal

from athena.capabilities.dispatcher import CapabilityDispatcher
from athena.capabilities.fs import FilesystemCapability
from athena.capabilities.registry import CapabilityRegistry
from athena.policy.engine import PolicyEngine
from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)
from athena.protocol.tasks import (
    AutonomyLevel,
    CapabilityPolicy,
    ResourceBudget,
    TaskSpec,
    WorkspaceSpec,
)
from athena.tasks.delegation import _scope_policy, _scope_workspace, _merged_budget


class _RecordingExecutor:
    """Fake capability executor that counts invocations (BHV-043)."""

    def __init__(self, cap_id: str, effect: EffectClass) -> None:
        self.descriptor = CapabilityDescriptor(
            id=cap_id,
            description=cap_id,
            input_schema={"allow_extra": True, "properties": {}},
            effects=frozenset({effect}),
        )
        self.invocations = 0

    async def invoke(self, request, *, output_accumulator=None, context=None):
        from athena.protocol.capabilities import CapabilityResultStatus

        self.invocations += 1
        return CapabilityResult(
            request.call_id, request.capability_id, CapabilityResultStatus.OK, output="ok"
        )


def _ws(root: str) -> WorkspaceSpec:
    return WorkspaceSpec(id="ws", root=root)


def _execute_req(task="child-1") -> CapabilityRequest:
    return CapabilityRequest(
        capability_id="execute",
        arguments={"language": "shell", "code": "ls"},
        task_id=task,
    )


def _fs_write_req(task="child-1") -> CapabilityRequest:
    return CapabilityRequest(
        capability_id="fs",
        arguments={"operation": "write", "path": "root.txt", "content": "x"},
        task_id=task,
    )


async def test_child_policy_denies_execute_when_parent_denies_it():
    """Child (via _scope_policy) inherits parent's deny; execute stays DENIED."""
    parent_policy = CapabilityPolicy(allow=("fs",), deny=("execute",))
    child_policy = _scope_policy(
        TaskSpec(id="parent", objective="p", capability_policy=parent_policy)
    )
    assert child_policy.allow == ("fs",)
    assert "execute" in child_policy.deny

    reg = CapabilityRegistry()
    exc = _RecordingExecutor("execute", EffectClass.EXECUTE)
    reg.register(exc)
    dispatcher = CapabilityDispatcher(
        reg, PolicyEngine(profile=AutonomyLevel.AUTONOMOUS)
    )

    result = await dispatcher.dispatch(
        _execute_req(), workspace=_ws("/parent"), task_policy=child_policy
    )
    assert result.status == CapabilityResultStatus.FAILED
    assert exc.invocations == 0


async def test_child_workspace_scoped_under_parent_rejects_outside_write(tmp_path):
    parent_root = str(tmp_path / "parent")
    (tmp_path / "parent").mkdir()
    (tmp_path / "parent" / "child").mkdir()
    parent_ws = WorkspaceSpec(id="parent", root=parent_root)
    child_ws = WorkspaceSpec(id="child", root=str(tmp_path / "parent" / "child"))
    parent = TaskSpec(id="parent", objective="p", workspace=parent_ws)
    scoped = _scope_workspace(parent, child_ws)
    # The child root is nested under, and never equals/exceeds, the parent root.
    assert scoped.root.startswith(parent_root)
    assert scoped.root != parent_root


async def test_child_workspace_denies_write_outside_child_root(tmp_path):
    parent_root = tmp_path / "parent"
    parent_root.mkdir()
    child_root = parent_root / "child"
    child_root.mkdir()

    reg = CapabilityRegistry()
    fs_exec = FilesystemCapability()
    reg.register(fs_exec)
    dispatcher = CapabilityDispatcher(
        reg, PolicyEngine(profile=AutonomyLevel.AUTONOMOUS)
    )

    # Child writes to the parent anchor path which is OUTSIDE the child root.
    result = await dispatcher.dispatch(
        CapabilityRequest(
            capability_id="fs",
            arguments={
                "operation": "write",
                "path": str(child_root / ".." / "outside.txt"),
                "content": "x",
            },
            task_id="child-1",
        ),
        workspace=WorkspaceSpec(id="child", root=str(child_root)),
    )
    assert result.status == CapabilityResultStatus.FAILED


async def test_child_budget_derived_from_parent_cannot_exceed():
    parent = TaskSpec(
        id="parent",
        objective="p",
        resource_budget=ResourceBudget(max_cost_usd=Decimal("1.0")),
    )
    child_spec = TaskSpec(
        id="child",
        objective="c",
        resource_budget=ResourceBudget(max_cost_usd=Decimal("5.0")),
    )
    merged = _merged_budget(parent, child_spec.resource_budget,
                            default_depth=1, default_children=4)
    assert merged.max_cost_usd == Decimal("1.0")