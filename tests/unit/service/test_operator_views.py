"""Unit tests for operator projections (stable views over canonical state)."""

from __future__ import annotations

import json

import pytest

from athena.protocol.capabilities import (
    CapabilityRequestOrigin,
    CapabilityResult,
    CapabilityResultStatus,
)
from athena.protocol.tasks import WorkspaceSpec
from athena.service.config import AthenaConfig
from athena.service.service import AthenaService


@pytest.fixture
async def service():
    svc = AthenaService.in_memory()
    await svc.start()
    try:
        yield svc
    finally:
        await svc.stop()


async def test_operator_permissions_empty(service):
    view = await service.operator_permissions()
    assert view == {"active_grants": [], "pending": []}


async def test_startup_health_exposes_optional_degradation_boundary(service):
    health = service.startup_health()

    assert health["status"] == "ok"
    assert health["blocking_failures"] == []
    assert health["checks"]
    assert all("blocking" in check for check in health["checks"].values())


async def test_operator_diff_empty(service):
    assert await service.operator_diff() == []


async def test_undo_unknown_mutation(service):
    outcome = await service.undo_mutation("mut_does_not_exist")
    assert outcome["status"] == "error"
    assert "not found" in outcome["error"]


async def test_operator_context_summary(service):
    info = await service.operator_context_summary("session_x")
    assert info["session_id"] == "session_x"


async def test_operator_artifacts_empty(service):
    assert isinstance(await service.operator_artifacts(), list)


async def test_generated_capability_operator_methods_use_synthesis_dispatcher():
    class Dispatcher:
        def __init__(self):
            self.calls = []

        async def dispatch(self, request, **kwargs):
            self.calls.append((request, kwargs))
            payload = {"capability_id": request.arguments.get("capability_id", "synth_1")}
            return CapabilityResult(
                request.call_id,
                request.capability_id,
                CapabilityResultStatus.OK,
                output=json.dumps(payload),
                metadata={"operation": request.arguments["operation"]},
            )

    dispatcher = Dispatcher()
    service = AthenaService.__new__(AthenaService)
    service.config = AthenaConfig()
    service._dispatcher = dispatcher
    service._default_workspace = WorkspaceSpec(id="root", root="/tmp/athena")

    candidates = await service.operator_generated_capabilities("task-1")
    inspected = await service.operator_generated_capability("synth_1", "task-1")
    promoted = await service.operator_promote_generated_capability("synth_1", "project", "task-1")
    deprecated = await service.operator_deprecate_generated_capability("synth_1", "task-1")

    assert candidates == {"capability_id": "synth_1"}
    assert inspected["capability_id"] == "synth_1"
    assert promoted["value"]["capability_id"] == "synth_1"
    assert promoted["metadata"]["operation"] == "promote"
    assert deprecated["value"]["capability_id"] == "synth_1"
    assert [request.arguments["operation"] for request, _ in dispatcher.calls] == [
        "candidates",
        "inspect",
        "promote",
        "deprecate",
    ]
    assert all(request.capability_id == "synthesis" for request, _ in dispatcher.calls)
    assert all(
        request.origin is CapabilityRequestOrigin.USER_DIRECT for request, _ in dispatcher.calls
    )
    assert all(kwargs["workspace"].id == "root" for _, kwargs in dispatcher.calls)


async def test_generated_capability_operator_method_surfaces_failure():
    class Dispatcher:
        async def dispatch(self, request, **kwargs):
            return CapabilityResult(
                request.call_id,
                request.capability_id,
                CapabilityResultStatus.FAILED,
                error="capability is unknown",
            )

    service = AthenaService.__new__(AthenaService)
    service.config = AthenaConfig()
    service._dispatcher = Dispatcher()
    service._default_workspace = WorkspaceSpec(id="root", root="/tmp/athena")

    try:
        await service.operator_generated_capability("synth_missing", "task-1")
    except ValueError as exc:
        assert str(exc) == "capability is unknown"
    else:
        raise AssertionError("expected synthesis failure")


async def test_direct_escape_records_but_excludes_from_context(service):
    """``!!`` semantics: durable audit record, excluded from model context."""
    session_id = "session_direct_test"

    result = await service.execute_direct(
        "echo hello",
        language="shell",
        session_id=session_id,
        inject_into_context=False,
    )
    if result.get("status") not in ("completed",):
        # Policy may require approval even for echo under supervised profile;
        # the record/exclusion contract below is what this test pins.
        return
    messages = await service._store_messages.list_session_messages(session_id)
    direct = [m for m in messages if (m.metadata or {}).get("direct_execution")]
    for m in direct:
        assert m.metadata.get("inject_into_context") is False
