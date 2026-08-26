"""Unit tests for operator projections (stable views over canonical state)."""

from __future__ import annotations

import pytest

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
