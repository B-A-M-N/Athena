"""Context failures must remain truthful and observable.

Graceful degradation is correct for optional memory/skill sources, but the
canonical transcript is authority-bearing and cannot be replaced with an
empty context after a read failure.
"""

from __future__ import annotations

import pytest

from athena.context.compiler import ContextCompiler
from athena.protocol.errors import ContextIntegrityError
from athena.protocol.tasks import TaskSpec


def _task(**kw) -> TaskSpec:
    return TaskSpec(id="task-degrade", objective="do the thing", **kw)


class _BrokenMemoryStore:
    """Every search path raises — the compiler must degrade, not crash."""

    generation = 1

    async def search(self, *a, **kw):
        raise RuntimeError("memory backend down")


class _EmptyMemoryStore:
    """A healthy store that simply has nothing — must NOT be a degradation."""

    generation = 1

    async def search(self, *a, **kw):
        return []


class _BrokenTranscriptStore:
    async def list_session_messages(self, session_id):
        raise OSError("transcript store unavailable")


class _BrokenSkillLoader:
    async def load_active(self):
        raise ValueError("skill index corrupted")


@pytest.mark.athena_claim("OBS-006")
async def test_failing_memory_store_records_degradation():
    compiler = ContextCompiler(memory_store=_BrokenMemoryStore())
    compiled = await compiler.compile(_task())
    sources = {d.source for d in compiled.degradations}
    assert "memory" in sources
    for d in compiled.degradations:
        if d.source == "memory":
            assert "RuntimeError" in d.detail
            assert "memory backend down" in d.detail
            assert len(d.detail) <= 256


@pytest.mark.athena_claim("OBS-006")
async def test_empty_memory_store_is_not_a_degradation():
    """The whole point of P1-6: absence must stay distinguishable from
    failure. A healthy store returning [] records nothing."""
    compiler = ContextCompiler(memory_store=_EmptyMemoryStore())
    compiled = await compiler.compile(_task())
    assert compiled.degradations == ()


@pytest.mark.athena_claim("OBS-006")
async def test_failing_transcript_raises_context_integrity_error():
    compiler = ContextCompiler(
        message_store=_BrokenTranscriptStore(),
        skill_loader=_BrokenSkillLoader(),
    )
    task = _task(session_id="sess-1")
    with pytest.raises(ContextIntegrityError, match="canonical transcript"):
        await compiler.compile(task)


@pytest.mark.athena_claim("OBS-006")
async def test_degradation_ledger_is_drained_per_compile():
    """Degradations belong to the compile that observed them; a second
    compile with a healthy store must not inherit stale failures."""
    compiler = ContextCompiler(memory_store=_BrokenMemoryStore())
    first = await compiler.compile(_task())
    assert first.degradations
    compiler._memory_store = _EmptyMemoryStore()
    second = await compiler.compile(_task())
    assert second.degradations == ()


@pytest.mark.athena_claim("OBS-006")
async def test_degradation_ledger_is_bounded():
    """A persistently failing store between compiles must not grow the
    ledger without limit; overflow is summarized, never lost silently in
    an unbounded way."""
    compiler = ContextCompiler(memory_store=_BrokenMemoryStore())
    for _ in range(100):
        compiler._record_degradation("memory", RuntimeError("x"))
    drained = compiler._drain_degradations()
    assert len(drained) <= 65
    assert any(d.source == "ledger" for d in drained)
    assert any("dropped" in d.detail for d in drained)
