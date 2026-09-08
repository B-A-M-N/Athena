"""Precompute-before-inference and hierarchical disclosure (P1).

The precompute path only WARMS the compiler's static cache; it must never
change what a later real compile produces. And the ``capabilities``
reflection affordance must stay on every work-bearing surface so the model
can expand what is shown without a discovery round-trip.
"""

from __future__ import annotations

import pytest

from athena.context.compiler import ContextCompiler
from athena.protocol.capabilities import CapabilityDescriptor
from athena.protocol.tasks import (
    ModelPolicy,
    TaskSpec,
    WorkspaceSpec,
)


class _MiniRegistry:
    """Minimal capability registry exposing the search surface.

    ``generation`` (an int) is what signals a cacheable, revisioned store —
    without it the compiler correctly refuses to cache, because a legacy
    mutable double must never be cached.
    """

    generation = 0

    def __init__(self, descriptors):
        self._by_id = {d.id: d for d in descriptors}

    def list_descriptors(self, **kwargs):
        return list(self._by_id.values())

    def list_available(self, **kwargs):
        return list(self._by_id.values())

    def search(self, query, **kwargs):
        # Match ids that appear in the query; always let reflection through.
        result = [
            {"id": d.id} for d in self._by_id.values() if d.id in query or d.id == "capabilities"
        ]
        return result


def _cap(id_: str) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        id=id_,
        description=id_,
        input_schema={"type": "object", "properties": {}},
        effects=frozenset(),
    )


_DESCRIPTORS = (
    _cap("capabilities"),
    _cap("fs"),
    _cap("git"),
    _cap("execute"),
    _cap("research"),
    _cap("skills"),
)


def _task(objective: str) -> TaskSpec:
    return TaskSpec(
        id="t-precompute",
        objective=objective,
        session_id="s1",
        workspace=WorkspaceSpec(id="w1", root="/tmp/ws"),
        model_policy=ModelPolicy(),
    )


def _compiler() -> ContextCompiler:
    return ContextCompiler(capability_registry=_MiniRegistry(_DESCRIPTORS))


# ---------------------------------------------------------------------- #
# Reflection affordance always available on work-bearing turns
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_resolved_surface_keeps_capabilities_reflection():
    """On a search HIT, the small selected surface still carries reflection."""
    compiler = _compiler()
    context = await compiler.compile(_task("read the README with fs"))
    visible = {d.id for d in context.capability_definitions}
    assert "fs" in visible, "work-bearing turn should keep its workspace primitive"
    assert "capabilities" in visible, "reflection must never leave a work surface"


@pytest.mark.asyncio
async def test_fallback_bundle_keeps_capabilities_reflection():
    """On a search MISS, the bounded fallback keeps reflection too."""
    compiler = _compiler()
    context = await compiler.compile(_task("do the weird thing now please"))
    visible = {d.id for d in context.capability_definitions}
    assert visible, "a tool-eligible turn compiles a working surface"
    assert "capabilities" in visible, "reflection must survive a miss"


@pytest.mark.asyncio
async def test_response_only_turn_gets_no_surface():
    ctx = _compiler()
    context = await ctx.compile(_task("just say hi and nothing else"))
    assert context.capability_definitions == ()


# ---------------------------------------------------------------------- #
# Precompute is a pure accelerator: same cache, same result
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_precompute_warms_then_compile_matches():
    compiler = _compiler()
    task = _task("list the repo state with git")
    # First: compile cold.
    cold = await compiler.compile(task)
    cold_visible = {d.id for d in cold.capability_definitions}

    prefetcher = _compiler()
    warmed = await prefetcher.precompute_static(task)
    assert warmed is True, "revisioned stores should produce a cacheable entry"
    # Now compile after prewarm — identical surface.
    hot = await prefetcher.compile(task)
    hot_visible = {d.id for d in hot.capability_definitions}
    assert hot_visible == cold_visible


@pytest.mark.asyncio
async def test_precompute_never_fails_admission():
    """A broken store must make prefetch a no-op, not an error."""
    compiler = _compiler()

    async def _boom(task):
        raise RuntimeError("store down")

    compiler._load_context_blocks = _boom  # type: ignore[attr-defined]
    assert await compiler.precompute_static(_task("anything here")) is False
    # Normal compile still surfaces the error to its caller.
    with pytest.raises(RuntimeError, match="store down"):
        await compiler.compile(_task("anything here"))
