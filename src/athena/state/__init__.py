"""Durable state public API with lazy exports.

State modules import one another through the shared ``Database`` type.  The
old eager package initializer imported every store while a submodule was
still initializing, creating a circular-import failure in short-lived worker
processes.  Keep the package boundary import-light and load an individual
store only when its public name is requested.
"""

from importlib import import_module
from typing import Any


_EXPORTS = {
    "Database": ("athena.state.database", "Database"),
    "SessionSpec": ("athena.state.sessions", "SessionSpec"),
    "SessionRepository": ("athena.state.sessions", "SessionRepository"),
    "TaskRepository": ("athena.state.sessions", "TaskRepository"),
    "EventRepository": ("athena.state.sessions", "EventRepository"),
    "MessageStore": ("athena.state.messages", "MessageStore"),
    "TaskStore": ("athena.state.tasks", "TaskStore"),
    "EventStore": ("athena.state.events", "EventStore"),
    "ExternalEffectStore": ("athena.state.external_effects", "ExternalEffectStore"),
    "RuntimeSessionStore": ("athena.state.runtime_sessions", "RuntimeSessionStore"),
    "ResourceObligationStore": (
        "athena.state.resource_obligations",
        "ResourceObligationStore",
    ),
    "ExecutionStore": ("athena.state.executions", "ExecutionStore"),
    "ApprovalStore": ("athena.state.approvals", "ApprovalStore"),
    "MutationStore": ("athena.state.mutations", "MutationStore"),
    "ScheduleStore": ("athena.state.schedules", "ScheduleStore"),
}


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value


__all__ = [
    "Database",
    "SessionSpec",
    "SessionRepository",
    "TaskRepository",
    "EventRepository",
    "MessageStore",
    "TaskStore",
    "EventStore",
    "ExternalEffectStore",
    "RuntimeSessionStore",
    "ResourceObligationStore",
    "ExecutionStore",
    "ApprovalStore",
    "MutationStore",
    "ScheduleStore",
]
