"""Durable compatibility receipts cover single and parallel dispatch paths."""
from __future__ import annotations

import pytest

from athena.capabilities.dispatcher import CapabilityDispatcher
from athena.capabilities.registry import CapabilityRegistry
from athena.policy.engine import PolicyEngine
from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)
from athena.protocol.tasks import AutonomyLevel, WorkspaceSpec
from athena.state.database import Database
from athena.state.tool_repairs import ToolRepairStore


class _Executor:
    descriptor = CapabilityDescriptor(
        id="files.read",
        description="read a file",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
            "x-athena-aliases": {"path": ["file_path"]},
        },
        effects=frozenset({EffectClass.READ_LOCAL}),
    )

    async def invoke(self, request, *, output_accumulator=None, context=None):
        return CapabilityResult(
            request.call_id, request.capability_id,
            CapabilityResultStatus.OK, output=request.arguments["path"],
        )


@pytest.mark.asyncio
async def test_parallel_repair_receipt_is_durable_and_replayable(tmp_path):
    db = Database(":memory:")
    store = ToolRepairStore(db)
    registry = CapabilityRegistry()
    registry.register(_Executor())
    dispatcher = CapabilityDispatcher(
        registry,
        PolicyEngine(AutonomyLevel.OFFLINE),
        repair_store=store,
    )

    request = CapabilityRequest(
        capability_id="files.read",
        arguments={"file_path": str(tmp_path / "a.txt")},
        task_id="task-repair",
        call_id="call-repair",
    )
    results = await dispatcher.dispatch_many(
        [request], workspace=WorkspaceSpec(id="repo", root=str(tmp_path))
    )

    assert results[0].status is CapabilityResultStatus.OK
    record = await store.get("call-repair")
    assert record is not None
    assert record["outcome"] == "REPAIRED"
    assert record["original_arguments"] == {
        "file_path": str(tmp_path / "a.txt")
    }
    assert record["canonical_arguments"] == {
        "path": str(tmp_path / "a.txt")
    }
    await db.close()
