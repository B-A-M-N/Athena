"""Direct and batched dispatch must use one canonical preparation contract."""

from __future__ import annotations

import asyncio

from athena.capabilities.dispatcher import CapabilityDispatcher
from athena.capabilities.prepared import PreparedCapabilityCall
from athena.capabilities.registry import CapabilityRegistry
from athena.models.compat.candidates import ToolCallCandidate, record_raw_candidate
from athena.policy.engine import PolicyEngine
from athena.protocol.capabilities import (
    CapabilityDescriptor,
    DispatchProvenance,
    CapabilityRequest,
    CapabilityRequestOrigin,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)
from athena.protocol.tasks import WorkspaceSpec


class _RecordingExecutor:
    def __init__(self) -> None:
        self.descriptor = CapabilityDescriptor(
            id="alias.read",
            description="read with an accepted model argument alias",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "x-athena-aliases": {"path": ["file_path"]},
            },
            effects=frozenset({EffectClass.READ_LOCAL}),
        )
        self.invocations = []

    async def invoke(self, request, *, output_accumulator=None, context=None):
        self.invocations.append(request)
        return CapabilityResult(
            request.call_id,
            request.capability_id,
            CapabilityResultStatus.OK,
            output=request.arguments["path"],
        )


def _workspace(tmp_path) -> WorkspaceSpec:
    return WorkspaceSpec(id="parity", root=str(tmp_path))


def _model_call(call_id: str) -> CapabilityRequest:
    request = CapabilityRequest(
        capability_id="alias.read",
        arguments={"file_path": "state.txt"},
        task_id="parity-task",
        call_id=call_id,
        origin=CapabilityRequestOrigin.MODEL,
    )
    record_raw_candidate(
        ToolCallCandidate(
            call_id=call_id,
            capability_id="alias.read",
            raw_arguments='{"file_path": "state.txt"}',
            parsed_arguments=None,
            completion_state="CLEAN",
        )
    )
    return request


async def _run(tmp_path):
    executor = _RecordingExecutor()
    registry = CapabilityRegistry()
    registry.register(executor)
    dispatcher = CapabilityDispatcher(registry, PolicyEngine("autonomous"))
    direct = await dispatcher.dispatch(
        _model_call("direct"),
        workspace=_workspace(tmp_path),
        provenance=DispatchProvenance(repair_mode="safe"),
    )
    batched = await dispatcher.dispatch_many(
        [_model_call("batched")],
        workspace=_workspace(tmp_path),
    )
    return executor, direct, batched[0]


async def test_preparation_failure_has_no_executable_snapshot(tmp_path):
    executor = _RecordingExecutor()
    registry = CapabilityRegistry()
    registry.register(executor)
    dispatcher = CapabilityDispatcher(registry, PolicyEngine("autonomous"))
    invalid = CapabilityRequest(
        "alias.read",
        {},
        task_id="t",
        call_id="invalid",
        origin=CapabilityRequestOrigin.MODEL,
    )
    record_raw_candidate(
        ToolCallCandidate(
            call_id="invalid",
            capability_id="alias.read",
            raw_arguments="{}",
            parsed_arguments={},
            completion_state="CLEAN",
        )
    )

    prepared = await dispatcher.prepare_one(invalid, _workspace(tmp_path))

    assert isinstance(prepared, PreparedCapabilityCall)
    assert prepared.executor is None
    assert prepared.failure is not None
    assert prepared.failure.code in {"repair_invalid", "schema_validation"}
    assert prepared.descriptor is None
    assert prepared.effects == ()
    assert prepared.resource_keys == ()
    assert executor.invocations == []


def test_model_alias_repair_is_identical_through_direct_and_batch_paths(tmp_path):
    executor, direct, batched = asyncio.run(_run(tmp_path))

    assert isinstance(direct, CapabilityResult)
    assert direct.status is CapabilityResultStatus.OK, direct.error
    assert isinstance(batched, CapabilityResult)
    assert batched.status is CapabilityResultStatus.OK, batched.error
    assert [r.arguments for r in executor.invocations] == [
        {"path": "state.txt"},
        {"path": "state.txt"},
    ]
    assert direct.output == batched.output == "state.txt"
