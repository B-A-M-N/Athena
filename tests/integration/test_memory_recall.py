"""Memory scope isolation across sessions, through the real MemoryStore +
ContextCompiler (the same path the kernel uses to build context)."""
from __future__ import annotations
import pytest

from athena.protocol.ids import new_id
from athena.protocol.memory import MemoryKind, MemoryRecord, MemoryScope
from athena.protocol.tasks import ResourceBudget, TaskSpec

_MARKER = "ATOMIC_LEVER_SENTINEL"


async def _compile_task(svc, objective: str, session_id: str):
    spec = TaskSpec(id=new_id("task"), objective=objective, session_id=session_id,
                    resource_budget=ResourceBudget(), metadata={"autonomy": "supervised"})
    compiled = await svc._compiler.compile(spec)
    return "\n".join(m.text() for m in compiled.messages)


async def _seed_session_memory(svc, session_id: str, content: str) -> None:
    await svc._memory.save(MemoryRecord(
        id=new_id("mem"),
        kind=MemoryKind.SEMANTIC,
        scope=MemoryScope.SESSION,
        content=content,
        metadata={"scope_id": session_id},
    ))


@pytest.mark.athena_claim("BHV-099")
@pytest.mark.athena_evidence("test", "e2e")
async def test_memory_recalled_in_session_but_not_other(make_service):
    svc = await make_service()
    session_a = new_id("session")
    session_b = new_id("session")
    await _seed_session_memory(
        svc, session_a, f"{_MARKER} is a durable fact about session A")

    context_a = await _compile_task(svc, "recall about the marker", session_a)
    context_b = await _compile_task(svc, "recall about the marker", session_b)

    assert _MARKER in context_a, "session-A memory must be recalled into context"
    assert _MARKER not in context_b, "session-A memory must NOT leak into session B"

    # Sanity: the memory really lives only in session A's scope.
    scoped = await svc._memory.list_by_scope(MemoryScope.SESSION, session_a)
    assert len(scoped) == 1