from __future__ import annotations

import pytest

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
from athena.protocol.tasks import AutonomyLevel, PathRule, WorkspaceSpec
from athena.state.database import Database
from athena.state.mutations import MutationStore

_TASK = (
    "INSERT INTO tasks(id, status, autonomy, objective, created_at, updated_at) "
    "VALUES ('t1', 'RUNNING', 'supervised', 'o', '2020-01-01T00:00:00Z', "
    "'2020-01-01T00:00:00Z')"
)


def _req(cap="fs", **args) -> CapabilityRequest:
    return CapabilityRequest(capability_id=cap, arguments=args or {}, task_id="t1")


def _ws(tmp_path) -> WorkspaceSpec:
    return WorkspaceSpec(id="w", root=str(tmp_path), writable=(PathRule(str(tmp_path)),))


class _FailingStore(MutationStore):
    async def record(self, *a, **k) -> str:
        raise RuntimeError("ledger down")


async def test_dispatcher_inject_stores_into_executor(tmp_path):
    store = MutationStore(Database(":memory:"))
    fs = FilesystemCapability(workspace=_ws(tmp_path))
    dispatcher = CapabilityDispatcher(
        CapabilityRegistry(),
        PolicyEngine(AutonomyLevel.AUTONOMOUS),
        mutation_store=store,
        artifact_store="fake-artifact-store",
    )
    dispatcher._inject_stores(fs)
    assert getattr(fs, "mutation_store", None) is store
    assert getattr(fs, "artifact_store", None) == "fake-artifact-store"


class _MutationExecutor:
    def __init__(self, descriptor):
        self.descriptor = descriptor

    async def invoke(self, request, *, output_accumulator=None, context=None):
        return CapabilityResult(
            request.call_id,
            request.capability_id,
            CapabilityResultStatus.OK,
            output="done",
            metadata={
                "mutation": {
                    "resource": "/tmp/x.txt",
                    "operation": "write",
                    "before_hash": "b",
                    "after_hash": "a",
                    "reversible": True,
                }
            },
        )


async def test_dispatcher_completion_record_failure_does_not_silently_succeed(tmp_path):
    db = Database(":memory:")
    await db.execute(_TASK)
    try:
        store = _FailingStore(db)
        exec_ = _MutationExecutor(
            CapabilityDescriptor(
                id="files.modify",
                description="m",
                input_schema={"allow_extra": True},
                effects=frozenset({EffectClass.READ_LOCAL}),
            )
        )
        reg = CapabilityRegistry()
        reg.register(exec_)
        dispatcher = CapabilityDispatcher(
            reg, PolicyEngine(AutonomyLevel.SUPERVISED), mutation_store=store
        )
        with pytest.raises(RuntimeError):
            await dispatcher.dispatch(_req(cap="files.modify"), workspace=_ws(tmp_path))
    finally:
        await db.close()
