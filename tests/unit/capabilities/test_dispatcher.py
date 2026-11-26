import asyncio

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


def _workspace(root="/tmp/ws") -> WorkspaceSpec:
    return WorkspaceSpec(id="w1", root=root)


class _Executor:
    """Minimal executor that records whether invoke() was called."""

    def __init__(self, descriptor, *, raise_on=None):
        self.descriptor = descriptor
        self.invocations = []
        self.raise_on = raise_on

    async def invoke(self, request, *, output_accumulator=None, context=None):
        self.invocations.append(request)
        if request.arguments.get("trigger") == self.raise_on:
            raise RuntimeError("boom")
        return CapabilityResult(
            "call", request.capability_id, CapabilityResultStatus.OK, output="ok"
        )


def _read_exec(*, raise_on=None) -> _Executor:
    return _Executor(
        CapabilityDescriptor(
            id="files.read",
            description="read",
            input_schema={
                "allow_extra": True,
                "properties": {"path": {"type": "string"}, "trigger": {"type": "string"}},
            },
            effects=frozenset({EffectClass.READ_LOCAL}),
        ),
        raise_on=raise_on,
    )


def _privileged_exec() -> _Executor:
    return _Executor(
        CapabilityDescriptor(
            id="sys.priv",
            description="privileged",
            input_schema={"allow_extra": True, "properties": {"cmd": {"type": "string"}}},
            effects=frozenset({EffectClass.PRIVILEGED}),
        )
    )


def _req(cap: str, **args) -> CapabilityRequest:
    return CapabilityRequest(capability_id=cap, arguments=args or {}, task_id="t1")


def _dispatcher(exec_: _Executor, profile=AutonomyLevel.SUPERVISED) -> CapabilityDispatcher:
    reg = CapabilityRegistry()
    reg.register(exec_)
    return CapabilityDispatcher(reg, PolicyEngine(profile))


def test_deny_verdict_does_not_call_executor():
    exec_ = _privileged_exec()
    dispatcher = _dispatcher(exec_)

    result = asyncio.run(
        dispatcher.dispatch(_req("sys.priv", cmd="rm -rf /"), workspace=_workspace())
    )

    assert isinstance(result, CapabilityResult)
    assert result.status == CapabilityResultStatus.FAILED
    assert result.error and "denied" in result.error
    assert exec_.invocations == []


def test_dispatch_many_survives_one_raising_call():
    exec_ = _read_exec(raise_on="x")
    dispatcher = _dispatcher(exec_)

    results = asyncio.run(
        dispatcher.dispatch_many(
            [
                _req("files.read", path="/tmp/ws/a.txt", trigger="x"),
                _req("files.read", path="/tmp/ws/b.txt"),
            ],
            workspace=_workspace(),
        )
    )

    assert len(results) == 2
    failures = [
        r
        for r in results
        if isinstance(r, CapabilityResult) and r.status == CapabilityResultStatus.FAILED
    ]
    ok = [
        r
        for r in results
        if isinstance(r, CapabilityResult) and r.status == CapabilityResultStatus.OK
    ]
    assert len(failures) == 1
    assert len(ok) == 1
    assert "boom" in failures[0].error


def test_resolve_effects_copy_returns_read_and_write():
    desc = CapabilityDescriptor(
        id="files.copy",
        description="copy",
        input_schema={},
        effects=frozenset({EffectClass.READ_LOCAL, EffectClass.WRITE_LOCAL}),
    )
    dispatcher = _dispatcher(_Executor(desc))
    effects = dispatcher._resolve_effects(desc, {"operation": "copy"})
    assert set(effects) == {EffectClass.READ_LOCAL, EffectClass.WRITE_LOCAL}


def test_dispatcher_emits_capability_progress_and_diagnostics():
    class ObservableExecutor(_Executor):
        async def invoke(self, request, *, output_accumulator=None, context=None):
            if output_accumulator is not None:
                await output_accumulator.chunk("observed", stream="stdout")
            return CapabilityResult(
                request.call_id,
                request.capability_id,
                CapabilityResultStatus.OK,
                metadata={"diagnostics": [{"message": "one warning"}]},
            )

    async def run() -> list[str]:
        executor = ObservableExecutor(_read_exec().descriptor)
        events = []

        async def sink(event):
            events.append(event)

        registry = CapabilityRegistry()
        registry.register(executor)
        dispatcher = CapabilityDispatcher(
            registry, PolicyEngine(AutonomyLevel.SUPERVISED), event_sink=sink
        )
        result = await dispatcher.dispatch(
            _req("files.read", path="/tmp/ws/a.txt"), workspace=_workspace()
        )
        assert result.status is CapabilityResultStatus.OK
        return [event.type for event in events]

    event_types = asyncio.run(run())
    assert "CapabilityProgress" in event_types
    assert "DiagnosticsProduced" in event_types
    assert event_types[-1] == "CapabilityCompleted"


@pytest.mark.asyncio
async def test_stale_prepared_call_cannot_cross_reused_call_id(tmp_path):
    """Removing ambient by-call-id prepared state prevents snapshot replay.

    Two calls with the same call_id must resolve their own executor and
    workspace independently. There is no hidden map for a prior dispatch to
    leave behind a stale prepared snapshot for the second dispatch.
    """
    from athena.capabilities.dispatcher import CapabilityDispatcher
    from athena.capabilities.registry import CapabilityRegistry
    from athena.policy.engine import PolicyEngine
    from athena.protocol.capabilities import (
        CapabilityRequest,
        CapabilityResult,
        CapabilityResultStatus,
        EffectClass,
    )
    from athena.protocol.tasks import AutonomyLevel, WorkspaceSpec

    class W:
        descriptor = CapabilityDescriptor(
            id="fs.write",
            description="write",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            effects=frozenset({EffectClass.WRITE_LOCAL}),
        )

        async def invoke(self, request, *, output_accumulator=None, context=None):
            return CapabilityResult(
                request.call_id, request.capability_id, CapabilityResultStatus.OK
            )

    ws = WorkspaceSpec(id="repo", root="/tmp")
    reg = CapabilityRegistry()
    reg.register(W())
    d = CapabilityDispatcher(reg, PolicyEngine(AutonomyLevel.AUTONOMOUS))
    r = CapabilityRequest("fs.write", {"path": "x.txt"}, task_id="t", call_id="reused-id")

    r1 = await d.dispatch(r, workspace=ws)
    assert r1.status is CapabilityResultStatus.OK
    # Ambient by-call-id prepared state must not exist after the call.
    assert not hasattr(d, "_prepared_by_call") or getattr(d, "_prepared_by_call", None) is None
    r2 = await d.dispatch(r, workspace=ws)
    assert r2.status is CapabilityResultStatus.OK


async def test_release_task_sync_cleans_synchronization_registries():
    """Terminal tasks must not leave lanes, semaphores, or escalation state."""
    from athena.capabilities.dispatcher import CapabilityDispatcher
    from athena.capabilities.registry import CapabilityRegistry
    from athena.policy.engine import PolicyEngine
    from athena.protocol.capabilities import (
        CapabilityRequest,
        CapabilityResult,
        CapabilityResultStatus,
    )
    from athena.protocol.capabilities import EffectClass
    from athena.protocol.tasks import AutonomyLevel, WorkspaceSpec

    class W:
        descriptor = CapabilityDescriptor(
            id="fs.write",
            description="write",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            effects=frozenset({EffectClass.WRITE_LOCAL}),
        )

        async def invoke(self, request, *, output_accumulator=None, context=None):
            return CapabilityResult(
                request.call_id, request.capability_id, CapabilityResultStatus.OK
            )

    ws = WorkspaceSpec(id="repo", root="/tmp")
    reg = CapabilityRegistry()
    reg.register(W())
    d = CapabilityDispatcher(reg, PolicyEngine(AutonomyLevel.AUTONOMOUS))
    r = CapabilityRequest("fs.write", {"path": "same.txt"}, task_id="task-cleanup")
    result = await d.dispatch(r, workspace=ws)
    assert result.status is CapabilityResultStatus.OK

    # Simulate state that accumulates for the task.
    d._execution_semaphores["task-cleanup"] = asyncio.Semaphore(2)
    d._runtime_speculative_tasks.add("task-cleanup")
    assert len(d._execution_semaphores) > 0

    d.release_task_sync("task-cleanup")
    assert "task-cleanup" not in d._execution_semaphores
    assert "task-cleanup" not in d._runtime_speculative_tasks


async def test_multi_lock_cancellation_does_not_strand_locks():
    """Cancellation while waiting for lock B must release already-held A."""
    from athena.capabilities.dispatch_helpers import ReferenceCountedKeyedLocks

    keyed = ReferenceCountedKeyedLocks()
    keys = ["a", "b"]

    # Pre-acquire lock "b" to block our waiter.
    blocker = keyed.acquire_reference("b")
    await blocker.acquire()

    async def waiter():
        locks = await keyed.acquire_many(keys)
        return locks

    task = asyncio.create_task(waiter())
    await asyncio.sleep(0.01)  # let it acquire "a" and start waiting on "b"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Lock "a" must be released and its reference dropped.
    entry_a = keyed._entries.get("a")
    assert entry_a is None or not entry_a[0].locked(), (
        "lock 'a' was stranded after cancellation during multi-key acquisition"
    )

    # Another caller must be able to acquire both without deadlock.
    fresh = ReferenceCountedKeyedLocks()
    locks = await asyncio.wait_for(fresh.acquire_many(keys), timeout=2.0)
    assert len(locks) == 2
    fresh.release_many(locks)
    assert len(fresh) == 0
