"""Restart safety for persisted approval grants."""

from __future__ import annotations

import pytest

from athena.policy.approvals import args_digest
from athena.policy.engine import PolicyEngine
from athena.protocol.capabilities import EffectClass
from athena.protocol.policy import ApprovalScope, PolicyRequest, Principal
from athena.protocol.tasks import WorkspaceSpec
from athena.service.service import AthenaService


class _Approvals:
    async def list_granted(self):
        return [{
            "id": "apr-call",
            "task_id": "task-1",
            "metadata": {
                "args_digest": args_digest({"path": "a.txt"}),
                "capability_id": "fs",
                "call_id": "call-1",
                "effects": [EffectClass.WRITE_LOCAL.value],
            },
            "grant_scope": ApprovalScope.CALL.value,
            "grant_expires_at": None,
        }]


class _Continuations:
    async def unconsumed_for_approval(self, approval_id):
        return [{"approval_id": approval_id, "call_id": "call-1"}]


@pytest.mark.asyncio
async def test_rehydrate_call_grant_only_for_unconsumed_continuation():
    service = AthenaService.__new__(AthenaService)
    service._policy = PolicyEngine()

    await service._rehydrate_approval_grants(_Approvals(), _Continuations())

    request = PolicyRequest(
        principal=Principal("agent", "athena"),
        task_id="task-1",
        capability_id="fs",
        arguments={"path": "a.txt"},
        workspace=WorkspaceSpec(id="root", root="/tmp"),
        effects=frozenset({EffectClass.WRITE_LOCAL}),
        call_id="call-1",
    )
    assert service._policy.approvals.covers_request(request) is not None
    # CALL scope is single-use even after restart rehydration.
    assert service._policy.approvals.covers_request(request) is None
