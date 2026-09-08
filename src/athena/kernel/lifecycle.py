"""Task lifecycle helpers for AgentKernel (BUILDSPEC §15, §16).

The kernel orchestrates iteration; it does not own SQL. This facade is a thin,
kernel-facing view over the canonical :class:`~athena.tasks.manager.TaskManager`,
which owns status validation, transition, event emission, and result
persistence. ``TaskLifecycle`` exists so ``AgentKernel`` keeps its existing
`acquire -> assert_runnable -> transition` interface while delegating the
authority to ``TaskManager``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from athena.protocol.tasks import (
    CapabilityPolicy,
    ContextRef,
    Criterion,
    DeliverySpec,
    ModelPolicy,
    ResourceBudget,
    TaskSpec,
    TaskStatus,
    WorkspaceSpec,
)
from athena.protocol.task_codec import (
    decode_budget as _decode_budget_canonical,
    decode_capability_policy as _decode_capability_policy_canonical,
    decode_context_refs as _decode_context_refs_canonical,
    decode_criteria as _decode_criteria_canonical,
    decode_delivery as _decode_delivery_canonical,
    decode_model_policy as _decode_model_policy_canonical,
    decode_task_spec,
    decode_workspace as _decode_workspace_canonical,
)
from athena.state.tasks import TaskStore

from athena.tasks.manager import TaskManager

__all__ = [
    "TaskLifecycle",
    "deserialize_task",
]


@dataclass
class TaskLifecycle:
    """Kernel-facing facade over the canonical :class:`TaskManager`.

    The ``manager`` is REQUIRED — there is exactly one TaskManager in the system
    (INV-002), created by AthenaService and injected here. This facade NEVER
    constructs its own manager (that was the duplicate-authority bug).
    """

    manager: TaskManager
    store: TaskStore | None = None
    events: Any = None
    _initialised: bool = False

    def __post_init__(self) -> None:
        if self.store is None:
            object.__setattr__(self, "store", getattr(self.manager, "_store", None))
        if self.events is None:
            object.__setattr__(self, "events", getattr(self.manager, "_events", None))
        object.__setattr__(self, "_initialised", True)

    async def acquire(self, task_id: str) -> TaskSpec:
        """Load and atomically claim the task as RUNNING (delegated)."""
        return await self.manager.acquire(task_id)

    async def assert_runnable(self, task: TaskSpec) -> None:
        """Re-check the persisted status; raise if the task is not RUNNING."""
        await self.manager.assert_runnable(task)

    async def transition(
        self,
        task_id: str,
        new_status: TaskStatus,
        *,
        session_id: str | None = None,
    ) -> None:
        await self.manager.transition(task_id, new_status)

    def set_budget_tracker(self, budgets: Any) -> None:
        """Forward a late-bound budget authority to the manager (§19)."""
        self.manager.set_budget_tracker(budgets)

    def set_cancellation_manager(self, cancellations: Any) -> None:
        """Forward a late-bound cancellation authority to the manager (§20)."""
        self.manager.set_cancellation_manager(cancellations)

    async def finalize(self, task, *args: Any, **kwargs: Any):
        """Delegate terminal finalization to TaskManager (§18, §86).

        Returns the persisted :class:`TaskResult`. The manager owns the
        atomic transition + result persistence and subsequent event emission.
        """
        return await self.manager.finalize(task, *args, **kwargs)


# ---------------------------------------------------------------------------
# Row -> TaskSpec deserialization (durable reconstruction, BHV-026).
# ---------------------------------------------------------------------------


def deserialize_task(row: dict[str, Any]) -> TaskSpec:
    """Rebuild a ``TaskSpec`` from a ``TaskStore.get`` row."""
    return decode_task_spec(row, status=row.get("status"))


def _decode_criteria(raw: Any) -> tuple[Criterion, ...]:
    return _decode_criteria_canonical(raw)


def _decode_context_refs(raw: Any) -> tuple[ContextRef, ...]:
    return _decode_context_refs_canonical(raw)


def _decode_workspace(raw: Any) -> WorkspaceSpec | None:
    return _decode_workspace_canonical(raw)


def _decode_capability_policy(raw: Any) -> CapabilityPolicy:
    return _decode_capability_policy_canonical(raw)


def _decode_model_policy(raw: Any) -> ModelPolicy:
    return _decode_model_policy_canonical(raw)


def _decode_budget(raw: Any) -> ResourceBudget:
    return _decode_budget_canonical(raw)


def _decode_delivery(raw: Any) -> DeliverySpec | None:
    return _decode_delivery_canonical(raw)


def _opt_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)
