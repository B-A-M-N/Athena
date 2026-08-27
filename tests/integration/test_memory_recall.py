"""Memory scope isolation across sessions, through the real MemoryStore +
ContextCompiler (the same path the kernel uses to build context)."""
from __future__ import annotations
import json
import pytest

from athena.capabilities.dispatcher import SuspendedCall
from athena.protocol.capabilities import (
    CapabilityRequest,
    CapabilityRequestOrigin,
    CapabilityResultStatus,
)
from athena.protocol.ids import new_id
from athena.protocol.memory import MemoryKind, MemoryRecord, MemoryScope
from athena.protocol.tasks import ResourceBudget, TaskSpec, WorkspaceSpec

_MARKER = "ATOMIC_LEVER_SENTINEL"


async def _compile_task(svc, objective: str, session_id: str, workspace=None):
    spec = TaskSpec(id=new_id("task"), objective=objective, session_id=session_id,
                    workspace=workspace,
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


async def _dispatch_memory(svc, *, task_id: str, session_id: str | None,
                           workspace: WorkspaceSpec, profile="coding", **arguments):
    # Dispatcher event persistence correctly enforces task/session foreign
    # keys. Create the minimal owning records so this test exercises the real
    # service dispatcher rather than bypassing its event path.
    if session_id and await svc._sessions.get(session_id) is None:
        await svc._sessions.create(session_id)
    if await svc._store_tasks.get(task_id) is None:
        await svc._store_tasks.insert_task(
            task_id,
            session_id,
            None,
            "memory capability contract test",
            workspace=workspace,
        )
    result = await svc._dispatcher.dispatch(
        CapabilityRequest(
            capability_id="memory",
            arguments=arguments,
            task_id=task_id,
            session_id=session_id,
            origin=CapabilityRequestOrigin.MODEL,
        ),
        workspace=workspace,
        profile=profile,
    )
    assert not isinstance(result, SuspendedCall)
    return result


@pytest.mark.athena_claim("BHV-099")
@pytest.mark.athena_evidence("test", "e2e")
async def test_memory_capability_save_binds_session_and_returns_bounded_json(make_service):
    svc = await make_service()
    workspace = svc._default_workspace
    session_a = new_id("session")
    result = await _dispatch_memory(
        svc,
        task_id=new_id("task"),
        session_id=session_a,
        workspace=workspace,
        operation="save",
        content=f"{_MARKER} written through the capability",
        scope="session",
        tags=["audit"],
    )

    assert result.status is CapabilityResultStatus.OK
    record = await svc._memory.get(result.ref_uri.removeprefix("memory:"))
    assert record is not None
    assert record.scope is MemoryScope.SESSION
    assert record.source is not None
    assert record.source.source_type.value == "capability"
    assert record.metadata.get("scope_id") == session_a
    raw = await svc._db.fetch_one(
        "SELECT metadata FROM memories WHERE id = ?", (record.id,)
    )
    assert raw is not None
    assert json.loads(raw["metadata"])["_athena:scope_id"] == session_a


@pytest.mark.athena_claim("BHV-099")
@pytest.mark.athena_evidence("test", "e2e")
async def test_memory_capability_isolates_sessions_tasks_and_projects(make_service):
    svc = await make_service()
    workspace = svc._default_workspace
    session_a, session_b = new_id("session"), new_id("session")
    task_a, task_b = new_id("task"), new_id("task")

    await _dispatch_memory(
        svc, task_id=task_a, session_id=session_a, workspace=workspace,
        operation="save", content="session A private marker", scope="session",
    )
    await _dispatch_memory(
        svc, task_id=task_a, session_id=session_a, workspace=workspace,
        operation="save", content="task A private marker", scope="task",
    )
    await _dispatch_memory(
        svc, task_id=task_a, session_id=session_a, workspace=workspace,
        operation="save", content="project shared marker", scope="project",
    )

    in_a = await _dispatch_memory(
        svc, task_id=task_a, session_id=session_a, workspace=workspace,
        profile="supervised", operation="recall", query="private marker",
    )
    in_b = await _dispatch_memory(
        svc, task_id=task_b, session_id=session_b, workspace=workspace,
        profile="supervised", operation="recall", query="private marker",
    )
    assert "session A private marker" in in_a.output
    assert "task A private marker" in in_a.output
    assert "session A private marker" not in in_b.output
    assert "task A private marker" not in in_b.output

    mismatched = await _dispatch_memory(
        svc, task_id=task_b, session_id=session_b, workspace=workspace,
        profile="supervised", operation="search", query="private marker",
        scope="session", scope_id=session_a,
    )
    assert mismatched.status is CapabilityResultStatus.FAILED

    global_attempt = await _dispatch_memory(
        svc, task_id=task_b, session_id=session_b, workspace=workspace,
        profile="supervised", operation="search", query="private marker",
        scope="global",
    )
    assert global_attempt.status is CapabilityResultStatus.FAILED
    assert "explicit user authority" in (global_attempt.error or "")

    project_from_new_session = await _dispatch_memory(
        svc, task_id=task_b, session_id=session_b, workspace=workspace,
        profile="supervised", operation="search", query="project shared marker",
    )
    assert "project shared marker" in project_from_new_session.output


@pytest.mark.athena_claim("BHV-099")
@pytest.mark.athena_evidence("test", "e2e")
async def test_context_compiler_retrieves_capability_written_memory(make_service):
    svc = await make_service()
    workspace = svc._default_workspace
    session_id = new_id("session")
    await _dispatch_memory(
        svc, task_id=new_id("task"), session_id=session_id, workspace=workspace,
        operation="save", content=f"{_MARKER} is a compiler-visible fact about the marker",
        scope="session",
    )

    compiled = await _compile_task(
        svc, "recall about the marker", session_id, workspace=workspace
    )
    assert _MARKER in compiled
