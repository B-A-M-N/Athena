"""Durable store acquisition for the service startup transaction.

The service lifecycle coordinates ordering and health projection.  This module
owns construction of the durable store graph from one database identity and
returns explicit components for the later startup phases.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from typing import Any

from athena.affordances import GeneratedCapabilityStore
from athena.project.index.builder import ProjectIndexBuilder
from athena.project.index.coordinator import ProjectIndexCoordinator
from athena.project.index.store import ProjectIndexStore
from athena.service.config import DEFAULT_DB_PATH
from athena.state.approvals import ApprovalStore
from athena.state.database import Database
from athena.state.events import EventStore
from athena.state.failure_memory import FailureMemory
from athena.state.external_effects import ExternalEffectStore
from athena.state.messages import MessageStore
from athena.state.mutations import MutationStore
from athena.state.model_responses import ModelResponseStore
from athena.state.provider_usage import ProviderUsageStore
from athena.state.resource_obligations import ResourceObligationStore
from athena.state.schedules import ScheduleStore
from athena.state.sessions import SessionRepository
from athena.state.steering import TaskSteeringStore
from athena.state.task_finalizations import TaskFinalizationStore
from athena.state.tasks import TaskStore
from athena.workflows import WorkflowRunStore, WorkflowStore
from athena.worldstate import WorldStateStore


@dataclass
class StoreComponents:
    """Explicit outputs of durable state acquisition."""

    db: Database
    runtime_state_root: str
    sessions: SessionRepository
    tasks: TaskStore
    events: EventStore
    messages: MessageStore
    approvals: ApprovalStore
    mutations: MutationStore
    external_effect_store: ExternalEffectStore
    resource_obligation_store: ResourceObligationStore
    pending_finalization_store: TaskFinalizationStore
    schedules: ScheduleStore
    continuations: Any
    input_requests: Any
    steering_store: TaskSteeringStore
    world_state_store: WorldStateStore
    workflow_store: WorkflowStore
    workflow_run_store: WorkflowRunStore
    generated_store: GeneratedCapabilityStore
    research_store: Any
    provider_usage_store: ProviderUsageStore
    model_response_store: ModelResponseStore
    project_index_store: ProjectIndexStore
    project_index_builder: ProjectIndexBuilder
    project_index_coordinator: ProjectIndexCoordinator
    failure_memory: FailureMemory


async def acquire_store_components(
    *,
    db_path: str | None,
    startup_health: dict[str, Any],
) -> StoreComponents:
    """Open the database and build every durable store exactly once."""
    resolved_db_path = db_path or DEFAULT_DB_PATH()
    db = Database(resolved_db_path)
    try:
        await db._ensure_ready()  # noqa: SLF001 - migrations run once at startup
    except Exception as exc:
        diagnostics = await db.diagnostics()
        startup_health["checks"]["database"] = {
            "status": "recovery_required",
            "blocking": True,
            "error": f"{type(exc).__name__}: {exc}",
            "diagnostics": diagnostics,
        }
        raise
    startup_health["checks"]["database"] = {
        "status": "ok",
        "blocking": False,
        "diagnostics": await db.diagnostics(),
    }
    runtime_state_root = (
        tempfile.mkdtemp(prefix="athena-runtime-")
        if resolved_db_path == ":memory:"
        else os.path.join(os.path.dirname(os.path.abspath(resolved_db_path)), "fusion")
    )

    sessions = SessionRepository(db)
    tasks = TaskStore(db)
    events = EventStore(db)
    messages = MessageStore(db)
    approvals = ApprovalStore(db)
    mutations = MutationStore(db)
    external_effect_store = ExternalEffectStore(db)
    resource_obligation_store = ResourceObligationStore(db)
    pending_finalization_store = TaskFinalizationStore(db)
    schedules = ScheduleStore(db)
    continuations = _continuation_store(db)
    input_requests = _input_request_store(db)
    steering_store = TaskSteeringStore(db)
    world_state_store = WorldStateStore(db)
    workflow_store = WorkflowStore(db)
    workflow_run_store = WorkflowRunStore(db)
    generated_store = GeneratedCapabilityStore(db)
    research_store = _research_store(db)
    provider_usage_store = ProviderUsageStore(db)
    model_response_store = ModelResponseStore(db)
    project_index_store = ProjectIndexStore(db)
    project_index_builder = ProjectIndexBuilder()
    project_index_coordinator = ProjectIndexCoordinator(
        project_index_store,
        project_index_builder,
    )
    failure_memory = FailureMemory(db)

    return StoreComponents(
        db=db,
        runtime_state_root=runtime_state_root,
        sessions=sessions,
        tasks=tasks,
        events=events,
        messages=messages,
        approvals=approvals,
        mutations=mutations,
        external_effect_store=external_effect_store,
        resource_obligation_store=resource_obligation_store,
        pending_finalization_store=pending_finalization_store,
        schedules=schedules,
        continuations=continuations,
        input_requests=input_requests,
        steering_store=steering_store,
        world_state_store=world_state_store,
        workflow_store=workflow_store,
        workflow_run_store=workflow_run_store,
        generated_store=generated_store,
        research_store=research_store,
        provider_usage_store=provider_usage_store,
        model_response_store=model_response_store,
        project_index_store=project_index_store,
        project_index_builder=project_index_builder,
        project_index_coordinator=project_index_coordinator,
        failure_memory=failure_memory,
    )


def _continuation_store(db: Database) -> Any:
    from athena.kernel.continuations import ContinuationStore

    return ContinuationStore(db)


def _input_request_store(db: Database) -> Any:
    from athena.state.input_requests import InputRequestStore

    return InputRequestStore(db)


def _research_store(db: Database) -> Any:
    from athena.research import ResearchStore

    return ResearchStore(db)


__all__ = ["StoreComponents", "acquire_store_components"]
