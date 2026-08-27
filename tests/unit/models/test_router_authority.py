"""Single model-routing authority (audit P0.1 / FUSE-002 evidence).

The kernel MUST NOT construct its own ModelRouter: a ``router or
ModelRouter(registry)`` fallback would be a second construction site for the
routing authority. The router is a required, service-injected dependency.
"""

from __future__ import annotations

import pytest

from athena.context.compiler import ContextCompiler
from athena.kernel.kernel import AgentKernel
from athena.kernel.termination import TerminationEvaluator
from athena.models.providers.fake import FakeModelProvider
from athena.models.registry import ProviderRegistry
from athena.models.router import ModelRouter
from athena.state.database import Database
from athena.state.events import EventStore
from athena.state.messages import MessageStore
from athena.state.sessions import SessionRepository
from athena.state.tasks import TaskStore
from athena.tasks.manager import TaskManager


async def _base_stores():
    db = Database(":memory:")
    await db._ensure_ready()
    sessions = SessionRepository(db)
    tasks = TaskStore(db)
    events = EventStore(db)
    messages = MessageStore(db)
    manager = TaskManager(task_store=tasks, events=events, sessions=sessions)
    return db, tasks, events, messages, manager


def _kernel_kwargs(tasks, events, messages, manager, registry, *, router):
    return dict(
        task_store=tasks,
        events=events,
        task_manager=manager,
        messages=messages,
        registry=registry,
        context_compiler=ContextCompiler(message_store=messages),
        termination=TerminationEvaluator(),
        router=router,
    )


async def test_kernel_without_router_is_construction_error():
    """No fallback: omitting the router must fail loudly, not self-construct."""
    db, tasks, events, messages, manager = await _base_stores()
    registry = ProviderRegistry()
    registry.register("fake", FakeModelProvider(model="fake-1", provider="fake"))
    with pytest.raises(ValueError, match="injected ModelRouter"):
        AgentKernel(**_kernel_kwargs(tasks, events, messages, manager,
                                     registry, router=None))
    await db.close()


async def test_kernel_holds_the_injected_router_instance():
    """The kernel stores exactly the injected router object — no copy, no rebuild."""
    db, tasks, events, messages, manager = await _base_stores()
    registry = ProviderRegistry()
    registry.register("fake", FakeModelProvider(model="fake-1", provider="fake"))
    router = ModelRouter(registry)
    kernel = AgentKernel(**_kernel_kwargs(tasks, events, messages, manager,
                                          registry, router=router))
    assert kernel._router is router
    await db.close()
