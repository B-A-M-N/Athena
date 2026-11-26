"""Dispatch provenance is invocation state, not dispatcher state (P0).

Two concurrent tasks must be able to dispatch with different provider/model
identity without one task's repair receipts capturing the other's identity.
"""

from __future__ import annotations

import asyncio

import pytest

from athena.capabilities.dispatcher import CapabilityDispatcher
from athena.protocol.capabilities import DispatchProvenance
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


class _RepairExecutor:
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
            request.call_id,
            request.capability_id,
            CapabilityResultStatus.OK,
            output=request.arguments.get("path", ""),
        )


def _request(task_id: str, call_id: str) -> CapabilityRequest:
    return CapabilityRequest(
        capability_id="files.read",
        arguments={"file_path": "/tmp/x.txt"},
        task_id=task_id,
        call_id=call_id,
    )


@pytest.mark.asyncio
async def test_concurrent_dispatch_provenance_is_invocation_scoped():
    db = Database(":memory:")
    store = ToolRepairStore(db)
    registry = CapabilityRegistry()
    registry.register(_RepairExecutor())
    dispatcher = CapabilityDispatcher(
        registry,
        PolicyEngine(AutonomyLevel.OFFLINE),
        repair_store=store,
    )
    ws = WorkspaceSpec(id="repo", root="/tmp")

    barrier = asyncio.Barrier(2)

    async def dispatch_with(provenance: DispatchProvenance, task_id: str, call_id: str):
        await barrier.wait()
        results = await dispatcher.dispatch_many(
            [_request(task_id, call_id)],
            workspace=ws,
            provenance=provenance,
        )
        assert results[0].status is CapabilityResultStatus.OK
        return results[0]

    try:
        a, b = await asyncio.gather(
            dispatch_with(
                DispatchProvenance(provider_profile_id="profile-A", model_id="model-A"),
                "task-A",
                "call-A",
            ),
            dispatch_with(
                DispatchProvenance(provider_profile_id="profile-B", model_id="model-B"),
                "task-B",
                "call-B",
            ),
        )
        assert a is not None and b is not None

        record_a = await store.get("call-A")
        record_b = await store.get("call-B")
        assert record_a is not None
        assert record_b is not None
        assert record_a["provider_profile_id"] == "profile-A"
        assert record_a["model_id"] == "model-A"
        assert record_b["provider_profile_id"] == "profile-B"
        assert record_b["model_id"] == "model-B"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_dispatcher_has_no_mutable_provenance_state():
    """The dispatcher must not expose set_inference_provenance anymore."""
    dispatcher = CapabilityDispatcher(
        CapabilityRegistry(),
        PolicyEngine(AutonomyLevel.AUTONOMOUS),
    )
    assert not hasattr(dispatcher, "set_inference_provenance")
    assert not hasattr(dispatcher, "_provider_profile_id")
    assert not hasattr(dispatcher, "_model_id")
    assert not hasattr(dispatcher, "_repair_mode")
