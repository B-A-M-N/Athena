"""Security tests for CALL-scoped approval binding.

A CALL-scoped grant must be bound to the EXACT call_id and task_id that
originated the request. A different task or different call must NOT be able
to consume another's single-use CALL grant.
"""
from __future__ import annotations

import pytest

from athena.policy.approvals import ApprovalManager, args_digest
from athena.protocol.capabilities import EffectClass
from athena.protocol.policy import (
    ApprovalScope,
    PolicyRequest,
    Principal,
)
from athena.protocol.tasks import WorkspaceSpec


def _make_request(
    *,
    task_id="task-A",
    call_id="call-1",
    capability_id="fs",
    arguments=None,
) -> PolicyRequest:
    return PolicyRequest(
        principal=Principal("agent", "athena"),
        task_id=task_id,
        capability_id=capability_id,
        arguments=arguments or {"path": "/file.txt", "content": "data"},
        workspace=WorkspaceSpec(id="root", root="/tmp"),
        effects=frozenset({EffectClass.WRITE_LOCAL}),
        call_id=call_id,
    )


def test_call_grant_consumed_by_same_call():
    """The original call can use the grant once."""
    mgr = ApprovalManager()
    req = _make_request()
    digest = args_digest(req.arguments)

    aid = mgr.create_request(
        req.principal, ApprovalScope.CALL,
        capability="fs",
        effect="WRITE_LOCAL",
        task_id=req.task_id,
        args_digest=digest,
        call_id=req.call_id,
    )
    mgr.grant(aid)

    grant = mgr.covers_request(req)
    assert grant is not None, "same call should match"

    # Second use by same call should be rejected (single-use)
    grant2 = mgr.covers_request(req)
    assert grant2 is None, "CALL grant should be consumed after first use"


def test_call_grant_not_consumable_by_different_task():
    """Task B cannot consume Task A's CALL grant even with identical args."""
    mgr = ApprovalManager()
    req_a = _make_request(task_id="task-A", call_id="call-1")
    digest = args_digest(req_a.arguments)

    aid = mgr.create_request(
        req_a.principal, ApprovalScope.CALL,
        capability="fs",
        effect="WRITE_LOCAL",
        task_id="task-A",
        args_digest=digest,
        call_id="call-1",
    )
    mgr.grant(aid)

    # Task B makes the identical request with same args but different task_id
    req_b = _make_request(task_id="task-B", call_id="call-2")
    grant = mgr.covers_request(req_b)
    assert grant is None, "different task must not consume another's CALL grant"


def test_call_grant_not_consumable_by_different_call_id():
    """Same task, different call_id cannot consume the grant."""
    mgr = ApprovalManager()
    digest = args_digest({"path": "/file.txt", "content": "data"})

    aid = mgr.create_request(
        Principal("agent", "athena"), ApprovalScope.CALL,
        capability="fs",
        effect="WRITE_LOCAL",
        task_id="task-A",
        args_digest=digest,
        call_id="call-1",
    )
    mgr.grant(aid)

    # Same task, same args, but different call_id
    req_different_call = _make_request(task_id="task-A", call_id="call-2")
    grant = mgr.covers_request(req_different_call)
    assert grant is None, "different call_id must not consume the grant"


def test_call_grant_rejected_on_argument_substitution():
    """Even with same call_id+task_id, different args must fail."""
    mgr = ApprovalManager()
    digest = args_digest({"path": "/file.txt", "content": "original"})

    aid = mgr.create_request(
        Principal("agent", "athena"), ApprovalScope.CALL,
        capability="fs",
        effect="WRITE_LOCAL",
        task_id="task-A",
        args_digest=digest,
        call_id="call-1",
    )
    mgr.grant(aid)

    req_modified = _make_request(
        task_id="task-A",
        call_id="call-1",
        arguments={"path": "/file.txt", "content": "substituted"},
    )
    grant = mgr.covers_request(req_modified)
    assert grant is None, "modified args must not match the grant"


def test_task_scope_grant_still_works_without_call_id():
    """TASK-scoped grants do not require call_id matching."""
    mgr = ApprovalManager()
    digest = args_digest({"path": "/file.txt", "content": "data"})

    aid = mgr.create_request(
        Principal("agent", "athena"), ApprovalScope.TASK,
        capability="fs",
        effect="WRITE_LOCAL",
        task_id="task-A",
        args_digest=digest,
    )
    mgr.grant(aid)

    # Any call within the same task can use it (not single-use for CALL)
    req1 = _make_request(task_id="task-A", call_id="call-1")
    grant = mgr.covers_request(req1)
    assert grant is not None, "TASK scope should match any call in the task"

    # Same task, different call still works (TASK scope is reusable)
    req2 = _make_request(task_id="task-A", call_id="call-2")
    grant2 = mgr.covers_request(req2)
    assert grant2 is not None, "TASK scope should be reusable within the task"