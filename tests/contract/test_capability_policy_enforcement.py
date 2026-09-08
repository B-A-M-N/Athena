"""Contract: capability policy enforcement in the dispatcher (INV-004 / P0-7).

The task's CapabilityPolicy is a HARD ceiling: it can deny, and the global
policy can only narrow further, never expand task authority. A denied
capability NEVER reaches its executor.
"""

from __future__ import annotations

import pytest

from athena.capabilities.dispatcher import CapabilityDispatcher
from athena.capabilities.registry import CapabilityRegistry
from athena.policy.engine import PolicyDecision, PolicyVerdict
from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityRequest,
    CapabilityRequestOrigin,
    CapabilityResult,
    CapabilityResultStatus,
    CapabilityOrigin,
    EffectClass,
)
from athena.protocol.tasks import CapabilityPolicy, WorkspaceSpec


class _CountingExecutor:
    """Executor that records whether it was invoked (spy)."""

    def __init__(self, capability_id="fs"):
        self.invoked = 0
        self.descriptor = CapabilityDescriptor(
            id=capability_id,
            description="spy",
            input_schema={"type": "object", "properties": {}, "required": []},
            effects=frozenset({EffectClass.READ_LOCAL}),
            origin=CapabilityOrigin.NATIVE,
        )

    async def invoke(self, request, *, output_accumulator=None, context=None):
        self.invoked += 1
        return CapabilityResult(
            call_id=getattr(request, "call_id", ""),
            capability_id=self.descriptor.id,
            status=CapabilityResultStatus.OK,
            output="spy-ok",
        )


def _fs_request(task_id="task-1") -> CapabilityRequest:
    return CapabilityRequest(
        capability_id="fs",
        arguments={"operation": "read", "path": "x"},
        task_id=task_id,
    )


def _workspace() -> WorkspaceSpec:
    return WorkspaceSpec(id="ws", root="/tmp/ws")


def _dispatcher(engine, executor):
    reg = CapabilityRegistry()
    reg.register(executor)
    return CapabilityDispatcher(reg, engine)


@pytest.mark.athena_claim("BHV-004", "BHV-043")
@pytest.mark.athena_evidence("test", "invariant")
class TestPolicyEnforcement:
    async def test_task_deny_never_calls_executor(self):
        executor = _CountingExecutor()
        dispatcher = _dispatcher(_AllowEngine(), executor)
        result = await dispatcher.dispatch(
            _fs_request(),
            workspace=_workspace(),
            task_policy=CapabilityPolicy(deny=("fs",)),
        )
        assert result.status == CapabilityResultStatus.FAILED
        assert "denied" in (result.error or "").lower()
        assert executor.invoked == 0

    async def test_task_allow_fs_denies_execute(self):
        executor = _CountingExecutor("execute")
        dispatcher = _dispatcher(_AllowEngine(), executor)
        req = CapabilityRequest(
            capability_id="execute",
            arguments={"language": "shell", "code": "echo hi"},
            task_id="task-1",
        )
        result = await dispatcher.dispatch(
            req,
            workspace=_workspace(),
            task_policy=CapabilityPolicy(allow=("fs",)),
        )
        assert result.status == CapabilityResultStatus.FAILED
        assert executor.invoked == 0

    async def test_task_deny_beats_global_allow(self):
        executor = _CountingExecutor()
        dispatcher = _dispatcher(_AllowEngine(), executor)
        result = await dispatcher.dispatch(
            _fs_request(),
            workspace=_workspace(),
            task_policy=CapabilityPolicy(deny=("fs",)),
        )
        assert result.status == CapabilityResultStatus.FAILED
        assert executor.invoked == 0

    async def test_task_allow_cannot_expand_global_deny(self):
        executor = _CountingExecutor()
        dispatcher = _dispatcher(_DenyEngine(), executor)
        result = await dispatcher.dispatch(
            _fs_request(),
            workspace=_workspace(),
            task_policy=CapabilityPolicy(allow=("fs",)),
        )
        assert result.status == CapabilityResultStatus.FAILED
        assert executor.invoked == 0

    async def test_wildcard_deny_ceiling_blocks_model_calls(self):
        executor = _CountingExecutor()
        dispatcher = _dispatcher(_AllowEngine(), executor)
        result = await dispatcher.dispatch(
            _fs_request(),
            workspace=_workspace(),
            task_policy=CapabilityPolicy(deny=("*",)),
        )
        assert result.status == CapabilityResultStatus.FAILED
        assert executor.invoked == 0

    async def test_host_system_observation_is_not_narrowed_by_task_ceiling(self):
        """SYSTEM-origin calls are the host auditing the task's own state.

        Acceptance-criteria verification and internal memory recall dispatch
        with SYSTEM origin from kernel-owned code paths — never from model
        input or natural language. A task that denies every capability must
        still be verifiable against the criteria it declared, so the task
        ceiling does not apply to the host's own observation floor. The
        global policy engine still applies in full.
        """
        executor = _CountingExecutor()
        dispatcher = _dispatcher(_AllowEngine(), executor)
        request = CapabilityRequest(
            capability_id="fs",
            arguments={"operation": "stat", "path": "release-maintained.txt"},
            task_id="task-1",
            origin=CapabilityRequestOrigin.SYSTEM,
        )
        result = await dispatcher.dispatch(
            request,
            workspace=_workspace(),
            task_policy=CapabilityPolicy(deny=("*",)),
        )
        assert result.status != CapabilityResultStatus.FAILED
        assert executor.invoked == 1

    async def test_host_system_observation_still_bounded_by_global_policy(self):
        """The SYSTEM-origin exemption lifts only the task ceiling.

        The global policy engine keeps full authority: a globally denied
        capability stays denied even for host observation.
        """
        executor = _CountingExecutor()
        dispatcher = _dispatcher(_DenyEngine(), executor)
        request = CapabilityRequest(
            capability_id="fs",
            arguments={"operation": "stat", "path": "x"},
            task_id="task-1",
            origin=CapabilityRequestOrigin.SYSTEM,
        )
        result = await dispatcher.dispatch(
            request,
            workspace=_workspace(),
            task_policy=CapabilityPolicy(deny=("*",)),
        )
        assert result.status == CapabilityResultStatus.FAILED
        assert executor.invoked == 0


class _AllowEngine:
    approvals = None

    def evaluate(self, request, *, autonomy=None):
        return PolicyDecision(PolicyVerdict.ALLOW, "allow", "stub.allow", ())


class _DenyEngine:
    approvals = None

    def evaluate(self, request, *, autonomy=None):
        return PolicyDecision(PolicyVerdict.DENY, "stub deny", "stub.deny", ())
