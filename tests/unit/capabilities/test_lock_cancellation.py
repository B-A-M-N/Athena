"""Cancellation cleanup must track acquired ownership, not lock state."""

from __future__ import annotations

import asyncio

import pytest

from athena.capabilities.dispatch_helpers import ReferenceCountedKeyedLocks


async def _wait_until(predicate, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("cancellation barrier was not reached")
        await asyncio.sleep(0)


async def test_acquired_locks_are_released_and_foreign_locks_are_preserved():
    keyed = ReferenceCountedKeyedLocks()
    blocker = keyed.acquire_reference("b")
    await blocker.acquire()

    task = asyncio.create_task(keyed.acquire_many(["a", "b"]))
    await _wait_until(lambda: "a" in keyed._entries)
    assert task.done() is False

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert "a" not in keyed._entries
    assert keyed._entries["b"][0].locked() is True

    blocker.release()
    keyed.release_reference(blocker)

    locks = await asyncio.wait_for(keyed.acquire_many(["a", "b"]), timeout=1)
    keyed.release_many(locks)
    assert len(keyed) == 0


async def test_pre_reserved_references_are_released_for_unacquired_locks():
    """Direct dispatch cancellation drops reservations for all its lane locks."""
    from athena.capabilities.dispatcher import CapabilityDispatcher
    from athena.capabilities.registry import CapabilityRegistry
    from athena.policy.engine import PolicyEngine
    from athena.protocol.capabilities import (
        CapabilityDescriptor,
        CapabilityRequest,
        CapabilityRequestOrigin,
        CapabilityResult,
        CapabilityResultStatus,
        EffectClass,
    )
    from athena.protocol.tasks import WorkspaceSpec

    class Executor:
        def __init__(self):
            self.descriptor = CapabilityDescriptor(  # noqa: E501
                id="x",
                description="two-resource sync",
                input_schema={"type": "object"},
                effects=frozenset({EffectClass.WRITE_LOCAL}),
                resource_key_resolver=lambda args, workspace: ("a", "b"),
            )

        async def invoke(self, request, **_):
            return CapabilityResult(
                request.call_id, request.capability_id, CapabilityResultStatus.OK
            )

    registry = CapabilityRegistry()
    registry.register(Executor())
    dispatcher = CapabilityDispatcher(registry, PolicyEngine("autonomous"))
    assert dispatcher.registry.executor_for("x") is not None
    workspace = WorkspaceSpec(id="parity", root="/tmp")
    request = CapabilityRequest(
        "x", {}, task_id="t", call_id="sync", origin=CapabilityRequestOrigin.USER_DIRECT
    )
    prepared = await dispatcher.prepare_one(
        request,
        workspace,
    )
    assert prepared.executor is not None, prepared.failure
    assert prepared.resource_keys == ("a", "b")

    holder_ready = asyncio.Event()
    holder_release = asyncio.Event()

    async def hold_b() -> None:
        blocker = dispatcher._resource_locks.acquire_reference(("parity", "b"))
        await blocker.acquire()
        holder_ready.set()
        try:
            await holder_release.wait()
        finally:
            blocker.release()
            dispatcher._resource_locks.release_reference(blocker)

    holder = asyncio.create_task(hold_b())
    await holder_ready.wait()
    table = dispatcher._resource_locks._entries
    task = asyncio.create_task(dispatcher._dispatch_with_controls(prepared, workspace=workspace))
    await _wait_until(lambda: ("parity", "a") in table and table[("parity", "a")][0].locked())
    try:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert ("parity", "a") not in dispatcher._resource_locks._entries
        # The cancelled waiter never owned b. Its holder task must retain the
        # lock until that holder releases it itself.
        assert dispatcher._resource_locks._entries[("parity", "b")][1] == 1
        assert dispatcher._resource_locks._entries[("parity", "b")][0].locked() is True
        assert not holder.done()
    finally:
        holder_release.set()
        await holder
    assert ("parity", "b") not in dispatcher._resource_locks._entries


async def test_direct_dispatch_cancellation_releases_ambient_lane_reference():
    """Cancellation before invocation cannot strand the shared ordering lane."""
    from athena.capabilities.dispatcher import CapabilityDispatcher
    from athena.capabilities.registry import CapabilityRegistry
    from athena.policy.engine import PolicyEngine
    from athena.protocol.capabilities import (
        CapabilityDescriptor,
        CapabilityRequest,
        CapabilityRequestOrigin,
        CapabilityResult,
        CapabilityResultStatus,
        EffectClass,
    )
    from athena.protocol.tasks import WorkspaceSpec

    class Executor:
        descriptor = CapabilityDescriptor(
            id="ambient",
            description="ambient execution",
            input_schema={"type": "object"},
            effects=frozenset({EffectClass.EXECUTE}),
        )

        async def invoke(self, request, **_):
            return CapabilityResult(
                request.call_id, request.capability_id, CapabilityResultStatus.OK
            )

    registry = CapabilityRegistry()
    registry.register(Executor())
    dispatcher = CapabilityDispatcher(registry, PolicyEngine("autonomous"))
    workspace = WorkspaceSpec(id="ambient", root="/tmp")
    prepared = await dispatcher.prepare_one(
        CapabilityRequest(
            "ambient",
            {},
            task_id="t",
            call_id="ambient",
            origin=CapabilityRequestOrigin.USER_DIRECT,
        ),
        workspace,
    )

    lane_key = ("ambient", "ambient-order-lane")
    holder_ready = asyncio.Event()
    holder_release = asyncio.Event()

    async def hold_lane() -> None:
        blocker = dispatcher._order_lanes.acquire_reference(lane_key)
        await blocker.acquire()
        holder_ready.set()
        try:
            await holder_release.wait()
        finally:
            blocker.release()
            dispatcher._order_lanes.release_reference(blocker)

    holder = asyncio.create_task(hold_lane())
    await holder_ready.wait()
    task = asyncio.create_task(dispatcher._dispatch_with_controls(prepared, workspace=workspace))
    await _wait_until(lambda: dispatcher._order_lanes._entries[lane_key][1] == 2)

    try:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert dispatcher._order_lanes._entries[lane_key][1] == 1
        assert dispatcher._order_lanes._entries[lane_key][0].locked() is True
        assert not holder.done()
    finally:
        holder_release.set()
        await holder
    assert lane_key not in dispatcher._order_lanes._entries
