"""Approval-security invariants.

* BHV-045: a call-scoped approval id is single-use — a second ``covers_request``
  using the same grant no longer matches.
* TOCTOU guard (BHV-047): a grant is bound to the exact argument digest;
  resuming with different arguments does not match.
* BHV-043: a denied / absent approval must never cause execution — the
  capability executor is not called.
"""

from __future__ import annotations
import pytest

from athena.capabilities.dispatcher import CapabilityDispatcher
from athena.capabilities.registry import CapabilityRegistry
from athena.policy.approvals import ApprovalManager, args_digest
from athena.policy.engine import PolicyEngine
from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)
from athena.protocol.policy import ApprovalScope, PolicyRequest, Principal
from athena.protocol.tasks import AutonomyLevel, WorkspaceSpec


def _principal() -> Principal:
    return Principal("agent", "athena")


def _policy_request(cap="files.write", args=None) -> PolicyRequest:
    return PolicyRequest(
        principal=_principal(),
        task_id="t1",
        capability_id=cap,
        arguments=args if args is not None else {"path": "/ws/a", "content": "x"},
        workspace=WorkspaceSpec(id="w", root="/ws"),
        effects=frozenset({EffectClass.WRITE_LOCAL}),
    )


def _principal_request() -> PolicyRequest:
    return _policy_request()


def _write_req(content: str) -> PolicyRequest:
    return _policy_request(args={"path": "/ws/a", "content": content})


@pytest.mark.athena_claim("BHV-045")
@pytest.mark.athena_evidence("test", "invariant")
def test_call_scope_approval_is_single_use():
    """BHV-045: same call-scoped approval cannot cover a second identical request."""
    mgr = ApprovalManager()
    aid = mgr.create_request(
        _principal(),
        ApprovalScope.CALL,
        capability="files.write",
        effect="WRITE_LOCAL",
        task_id="t1",
    )
    mgr.grant(aid)

    req = _principal_request()
    first = mgr.covers_request(req)
    assert first is not None

    second = mgr.covers_request(req)
    assert second is None  # consumed on first use


@pytest.mark.athena_claim("BHV-047")
@pytest.mark.athena_evidence("test", "security")
def test_grant_binds_to_exact_arguments_toctou_guard():
    """BHV-047: substituting args makes a previously matching grant not apply."""
    mgr = ApprovalManager()
    aid = mgr.create_request(
        _principal(),
        ApprovalScope.CALL,
        capability="files.write",
        effect="WRITE_LOCAL",
        task_id="t1",
        args_digest=args_digest({"path": "/ws/a", "content": "secret"}),
    )
    mgr.grant(aid)

    # Same args -> covered.
    assert mgr.covers_request(_write_req(content="secret")) is not None

    # Different args (TOCTOU substitution) -> NOT covered.
    assert mgr.covers_request(_write_req(content="evil")) is None


@pytest.mark.athena_claim("BHV-044")
@pytest.mark.athena_evidence("test")
def test_denied_approval_grant_is_rejected():
    """BHV-043: a denied approval cannot be granted afterwards -> no grant."""
    mgr = ApprovalManager()
    aid = mgr.create_request(_principal(), ApprovalScope.CALL, capability="files.write")
    mgr.deny(aid)
    # grant() after deny is an illegal transition.
    from athena.policy.approvals import ApprovalError

    raised = False
    try:
        mgr.grant(aid)
    except ApprovalError:
        raised = True
    assert raised
    assert mgr.covers_request(_principal_request()) is None


class _DeniedExecutor:
    """Executor that should never run when the request is denied."""

    def __init__(self, cap_id: str, effect: EffectClass) -> None:
        self.descriptor = CapabilityDescriptor(
            id=cap_id,
            description=cap_id,
            input_schema={"allow_extra": True, "properties": {}},
            effects=frozenset({effect}),
        )
        self.invocations = 0

    async def invoke(self, request, *, output_accumulator=None, context=None):
        self.invocations += 1
        return CapabilityResult(
            request.call_id, request.capability_id, CapabilityResultStatus.OK, output="ok"
        )


@pytest.mark.athena_claim("BHV-043")
@pytest.mark.athena_evidence("test", "security")
async def test_deny_verdict_does_not_call_executor():
    """BHV-043 across the dispatcher: hard-deny never reaches the executor."""
    reg = CapabilityRegistry()
    exec_ = _DeniedExecutor("files.write", EffectClass.WRITE_LOCAL)
    reg.register(exec_)
    dispatcher = CapabilityDispatcher(reg, PolicyEngine(profile=AutonomyLevel.SUPERVISED))

    from athena.protocol.tasks import CapabilityPolicy

    result = await dispatcher.dispatch(
        CapabilityRequest(
            capability_id="files.write",
            arguments={"path": "/etc/x", "content": "y"},
            task_id="t1",
        ),
        workspace=WorkspaceSpec(id="w", root="/ws"),
        task_policy=CapabilityPolicy(deny=("files.write",)),
    )
    assert result.status == CapabilityResultStatus.FAILED
    assert exec_.invocations == 0
