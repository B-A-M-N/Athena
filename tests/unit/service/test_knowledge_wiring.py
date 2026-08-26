"""End-to-end wiring tests: knowledge pipeline, criteria, role selection."""

from __future__ import annotations

import asyncio

import pytest

from athena.service.service import AthenaService
from athena.protocol.tasks import TaskStatus


@pytest.fixture
async def service():
    svc = AthenaService.in_memory()
    await svc.start()
    try:
        yield svc
    finally:
        await svc.stop()


async def test_finalize_observer_registered(service):
    """The knowledge pipeline is bound to the single finalization authority."""
    observers = service._task_manager._finalize_observers
    assert any(type(o).__name__ == "KnowledgePipeline" for o in observers)


async def test_completed_task_records_episodic_memory(service):
    spec = await service.submit(_req("say the word banana"), wait=True)
    rows = await service._store_tasks.list_by_status(TaskStatus.COMPLETE)
    assert any(r["id"] == spec.id for r in rows)

    # The knowledge pipeline is async post-finalization; poll briefly.
    content: list[str] = []
    for _ in range(40):
        episodic = await service._memory.retrieve_by_recency(None, None, 100)
        content = [r.content for r in episodic]
        if any(spec.id in c or "banana" in c for c in content):
            return
        await asyncio.sleep(0.1)
    pytest.fail(f"episodic memory not recorded; got {content!r}")


def _req(prompt: str):
    from athena.protocol.tasks import AgentRequest

    return AgentRequest(prompt=prompt)


async def test_criteria_metadata_builds_required_criteria(service):
    request = _req("do the thing")
    request = type(request)(
        prompt=request.prompt,
        metadata={"acceptance_criteria": ["command:true", "the file exists"]},
    )
    session_id = "session_criteria"
    spec = service._build_task_spec(request, session_id)
    crits = spec.acceptance_criteria
    assert len(crits) == 2
    assert all(c.required for c in crits)
    assert crits[0].verification.command == "true"
    assert crits[1].verification.predicate == "the file exists"


async def test_role_parsing(service):
    policies = service._role_policies(
        {
            "summarizer": {"allowed": ["prov/cheap"], "max_cost_usd": "0.05"},
            "bad": "not-a-table",
        }
    )
    assert policies["summarizer"].allowed == ("prov/cheap",)
    assert "bad" not in policies
