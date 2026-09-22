"""Typed kernel failures remain typed through task events and native projection."""

from __future__ import annotations

import pytest

from athena.cli.native_bridge import native_projection_frame
from athena.cli.projection import ProjectionState
from athena.context.compiler import ContextCompiler
from athena.kernel.kernel import AgentKernel
from athena.kernel.termination import TerminationEvaluator
from athena.models.registry import ProviderRegistry
from athena.models.router import ModelRouter
from athena.protocol.errors import ModelUnavailable
from athena.protocol.ids import new_id
from athena.protocol.tasks import ResourceBudget, TaskSpec
from athena.state.database import Database
from athena.state.events import EventStore
from athena.state.messages import MessageStore
from athena.state.sessions import SessionRepository
from athena.state.tasks import TaskStore
from athena.tasks.manager import TaskManager
from athena.tasks.worker import TaskWorker


class _UnavailableRouter(ModelRouter):
    async def select(self, **_kwargs):
        raise ModelUnavailable("no eligible model for the request")


@pytest.mark.asyncio
async def test_model_unavailable_survives_kernel_worker_event_and_native_frame():
    db = Database(":memory:")
    await db._ensure_ready()
    sessions = SessionRepository(db)
    tasks = TaskStore(db)
    events = EventStore(db)
    messages = MessageStore(db)
    manager = TaskManager(task_store=tasks, events=events, sessions=sessions)
    router = _UnavailableRouter(ProviderRegistry())
    kernel = AgentKernel(
        task_store=tasks,
        events=events,
        task_manager=manager,
        messages=messages,
        registry=ProviderRegistry(),
        router=router,
        context_compiler=ContextCompiler(message_store=messages),
        termination=TerminationEvaluator(),
    )
    session_id = new_id("session")
    task = TaskSpec(
        id=new_id("task"),
        objective="route this request",
        session_id=session_id,
        resource_budget=ResourceBudget(max_agent_iterations=3),
    )
    await manager.create(task)
    await manager.enqueue(task.id)

    try:
        result = await TaskWorker(task_manager=manager, kernel=kernel).run_task(task.id)
        assert result.status.value == "FAILED"
        failed = next(
            event for event in await events.list_for_task(task.id) if event.type == "TaskFailed"
        )

        projection = ProjectionState()
        projection.reduce(failed.type, failed.payload)
        frame = native_projection_frame(projection, width=80, height=24)

        assert failed.payload["failure"] == {
            "reason": "no eligible model for the request",
            "stage": "model_admission",
            "kind": "model_routing",
            "code": "model_unavailable",
            "fatal": True,
        }
        assert frame["failure"] == failed.payload["failure"]
        assert frame["visual_mode"] == "failure"
    finally:
        await db.close()
