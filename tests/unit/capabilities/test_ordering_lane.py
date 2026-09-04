"""Dependency-aware parallel dispatch ordering (P1, task #10).

Rule: resource-less dependency-bearing calls (a write/delete/external acting
on ambient state, no concrete path) cannot prove independence, so they
serialize against each other in model order. Named-path writes and pure
reads keep their existing parallelism: different paths are provably
independent, same-path calls serialize via the per-resource lock.
"""

from __future__ import annotations

import asyncio

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
from athena.protocol.tasks import WorkspaceSpec


class _OrderedExecutor:
    """Records the interleaving of invocations it observes."""

    def __init__(self, descriptor, *, delay_ms=20):
        self.descriptor = descriptor
        self.delay = delay_ms / 1000.0
        self.observed_active = 0
        self.max_active = 0
        self.starts: list[str] = []
        self.mutex = asyncio.Lock()

    async def invoke(self, request, *, output_accumulator=None, context=None):
        async with self.mutex:
            self.observed_active += 1
            self.max_active = max(self.max_active, self.observed_active)
            self.starts.append(request.arguments.get("tag", request.capability_id))
        await asyncio.sleep(self.delay)
        async with self.mutex:
            self.observed_active -= 1
        return CapabilityResult(
            request.call_id, request.capability_id, CapabilityResultStatus.OK
        )


def _req(cap, **args) -> CapabilityRequest:
    return CapabilityRequest(
        capability_id=cap, arguments=args, task_id="t1"
    )


def _dispatcher(ex, profile="autonomous") -> CapabilityDispatcher:
    reg = CapabilityRegistry()
    reg.register(ex)
    return CapabilityDispatcher(reg, PolicyEngine(profile))


_AMBIENT = CapabilityDescriptor(
    id="ambient.write",
    description="resource-less mutation",
    input_schema={"allow_extra": True},
    effects=frozenset({EffectClass.WRITE_LOCAL}),
)
_NAMED = CapabilityDescriptor(
    id="named.write",
    description="named-path mutation",
    input_schema={"allow_extra": True},
    effects=frozenset({EffectClass.WRITE_LOCAL}),
)
_READ = CapabilityDescriptor(
    id="read",
    description="read",
    input_schema={"allow_extra": True},
    effects=frozenset({EffectClass.READ_LOCAL}),
)


def _run(coro):
    return asyncio.run(coro)


def test_named_path_writes_to_different_files_parallelize():
    """Different concrete paths are provably independent: may overlap."""
    ex = _OrderedExecutor(_NAMED, delay_ms=40)
    d = _dispatcher(ex)
    ws = WorkspaceSpec(id="w1", root="/tmp/ws")
    results = _run(
        d.dispatch_many(
            [
                _req("named.write", path="/tmp/ws/a", tag="a"),
                _req("named.write", path="/tmp/ws/b", tag="b"),
            ],
            workspace=ws,
        )
    )
    assert all(r.status == CapabilityResultStatus.OK for r in results)
    assert ex.max_active == 2, f"independent paths should run together, saw {ex.max_active}"


def test_same_path_writes_serialize():
    """Same concrete path remains serialized by the per-resource lock."""
    ex = _OrderedExecutor(_NAMED, delay_ms=40)
    d = _dispatcher(ex)
    ws = WorkspaceSpec(id="w1", root="/tmp/ws")
    _run(
        d.dispatch_many(
            [
                _req("named.write", path="/tmp/ws/shared", tag="a"),
                _req("named.write", path="/tmp/ws/shared", tag="b"),
            ],
            workspace=ws,
        )
    )
    assert ex.max_active == 1, f"same path must serialize, saw {ex.max_active}"


def test_pure_reads_parallelize():
    ex = _OrderedExecutor(_READ, delay_ms=40)
    d = _dispatcher(ex)
    ws = WorkspaceSpec(id="w1", root="/tmp/ws")
    _run(
        d.dispatch_many(
            [
                _req("read", path="/tmp/ws/a", tag="r1"),
                _req("read", path="/tmp/ws/b", tag="r2"),
                _req("read", path="/tmp/ws/c", tag="r3"),
            ],
            workspace=ws,
        )
    )
    assert ex.max_active == 3, f"independent reads should run together, saw {ex.max_active}"


def test_pure_reads_vs_mutations_do_not_race_on_shared_resource():
    """A mutation targeting a resource the batch also reads is serialized by
    the per-resource lock: the lane keeps read-after-write correctness for a
    shared concrete path without collapsing independent parallelism."""
    from athena.protocol.capabilities import CapabilityDescriptor as CD

    shared = CD(
        id="shared.resource",
        description="shared",
        input_schema={"allow_extra": True},
        effects=frozenset(
            {EffectClass.READ_LOCAL, EffectClass.WRITE_LOCAL}
        ),
    )
    ex = _OrderedExecutor(shared, delay_ms=30)
    d = _dispatcher(ex, profile="supervised")
    ws = WorkspaceSpec(id="w1", root="/tmp/ws")
    # read then write on the SAME path — must not overlap.
    _run(
        d.dispatch_many(
            [
                _req("shared.resource", operation="read", path="/tmp/ws/data", tag="read"),
                _req("shared.resource", operation="write", path="/tmp/ws/data", tag="write"),
            ],
            workspace=ws,
        )
    )
    assert ex.max_active == 1, f"shared-path read+write must not overlap, saw {ex.max_active}"
    assert ex.starts[0] == "read", f"model order preserved: {ex.starts}"

def test_execution_concurrency_is_governed_by_the_lease_not_the_order_lane():
    """EXECUTE/SPAWN stays OUT of the batch order lane so the execution-lease
    semaphore owns their concurrency volume (max_parallel_executions)."""
    ex = _OrderedExecutor(
        CapabilityDescriptor(
            id="slow-exec",
            description="exec",
            input_schema={"allow_extra": True},
            effects=frozenset({EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS}),
        ),
        delay_ms=40,
    )
    d = _dispatcher(ex, profile="autonomous")
    ws = WorkspaceSpec(id="w1", root="/tmp/ws")
    _run(
        d.dispatch_many(
            [_req("slow-exec", code="echo", tag=f"e{i}") for i in range(4)],
            workspace=ws,
            task_budget=None,
        )
    )
    assert ex.max_active >= 2, f"executes must parallelize under the lease, saw {ex.max_active}"