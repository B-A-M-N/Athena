"""Task lifecycle helpers for AgentKernel (BUILDSPEC §15, §16).

The kernel orchestrates iteration; it does not own SQL. This facade is a thin,
kernel-facing view over the canonical :class:`~athena.tasks.manager.TaskManager`,
which owns status validation, transition, event emission, and result
persistence. ``TaskLifecycle`` exists so ``AgentKernel`` keeps its existing
`acquire -> assert_runnable -> transition` interface while delegating the
authority to ``TaskManager``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from athena.protocol.tasks import (
    CapabilityPolicy,
    ContextRef,
    Criterion,
    DeliverySpec,
    ModelPolicy,
    NetworkPolicy,
    PathRule,
    ResourceBudget,
    TaskSpec,
    TaskStatus,
    VerificationSpec,
    VerificationType,
    WorkspaceSpec,
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
    deadline = _ds(row.get("deadline"))

    # Ground work on the persisted status so lifecycle events are accurate.
    metadata = dict(row.get("metadata") or {})
    metadata["status"] = row.get("status")

    return TaskSpec(
        id=row["id"],
        objective=row.get("objective") or "",
        session_id=row.get("session_id"),
        parent_task_id=row.get("parent_task_id"),
        acceptance_criteria=_decode_criteria(row.get("acceptance_criteria")),
        context_refs=_decode_context_refs(row.get("context_refs")),
        workspace=_decode_workspace(row.get("workspace")),
        capability_policy=_decode_capability_policy(row.get("capability_policy")),
        model_policy=_decode_model_policy(row.get("model_policy")),
        resource_budget=_decode_budget(row.get("resource_budget")),
        deadline=deadline,
        delivery=_decode_delivery(row.get("delivery")),
        metadata=metadata,
    )


def _ds(value: Any) -> datetime | None:
    from datetime import datetime as _dt
    if isinstance(value, _dt):
        return value
    if isinstance(value, str):
        try:
            return _dt.fromisoformat(value)
        except ValueError:
            return None
    return None


def _decode_criteria(raw: Any) -> tuple[Criterion, ...]:
    if not raw:
        return ()
    items = raw if isinstance(raw, list) else json.loads(raw)
    out = []
    for item in items:
        v = item.get("verification")
        verification = None
        if v:
            verification = VerificationSpec(
                type=VerificationType(v.get("type", "model_judgment")),
                command=v.get("command"),
                path=v.get("path"),
                predicate=v.get("predicate"),
                capability=v.get("capability"),
            )
        out.append(Criterion(
            id=item.get("id", ""),
            description=item.get("description", ""),
            verification=verification,
            required=bool(item.get("required", True)),
        ))
    return tuple(out)


def _decode_context_refs(raw: Any) -> tuple[ContextRef, ...]:
    if not raw:
        return ()
    items = raw if isinstance(raw, list) else json.loads(raw)
    out = []
    for item in items:
        out.append(ContextRef(
            kind=item.get("kind", "session"),
            ref=item.get("ref", ""),
            source_id=item.get("source_id"),
            summary=item.get("summary"),
        ))
    return tuple(out)


def _decode_workspace(raw: Any) -> WorkspaceSpec | None:
    if not raw:
        return None
    data = raw if isinstance(raw, dict) else json.loads(raw)
    return WorkspaceSpec(
        id=data.get("id", ""),
        root=data.get("root", ""),
        readable=tuple(
            PathRule(path=r.get("path", ""), allow=bool(r.get("allow", True)))
            for r in (data.get("readable") or [])
        ),
        writable=tuple(
            PathRule(path=r.get("path", ""), allow=bool(r.get("allow", True)))
            for r in (data.get("writable") or [])
        ),
        temp_root=data.get("temp_root"),
        execution_backend=data.get("execution_backend", "local"),
        network_policy=NetworkPolicy(data.get("network_policy", "allow")),
    )


def _decode_capability_policy(raw: Any) -> CapabilityPolicy:
    if not raw:
        return CapabilityPolicy()
    data = raw if isinstance(raw, dict) else json.loads(raw)
    return CapabilityPolicy(
        effects=frozenset(data.get("effects") or []),
        allow=tuple(data.get("allow") or []),
        ask=tuple(data.get("ask") or []),
        deny=tuple(data.get("deny") or []),
    )


def _decode_model_policy(raw: Any) -> ModelPolicy:
    if not raw:
        return ModelPolicy()
    data = raw if isinstance(raw, dict) else json.loads(raw)
    cost = data.get("max_cost_usd")
    return ModelPolicy(
        role=data.get("role", "primary"),
        allowed=tuple(data.get("allowed") or []),
        require_tools=bool(data.get("require_tools", True)),
        privacy=data.get("privacy", "local-preferred"),
        max_cost_usd=Decimal(str(cost)) if cost else None,
    )


def _decode_budget(raw: Any) -> ResourceBudget:
    if not raw:
        return ResourceBudget()
    data = raw if isinstance(raw, dict) else json.loads(raw)
    wall = data.get("max_wall_time")
    cost = data.get("max_cost_usd")
    return ResourceBudget(
        max_agent_iterations=int(data.get("max_agent_iterations", 100)),
        max_input_tokens=_opt_int(data.get("max_input_tokens")),
        max_output_tokens=_opt_int(data.get("max_output_tokens")),
        max_cost_usd=Decimal(str(cost)) if cost else None,
        max_wall_time=timedelta(seconds=float(wall)) if wall else None,
        max_children=int(data.get("max_children", 4)),
        max_child_depth=int(data.get("max_child_depth", 1)),
        max_parallel_model_calls=int(data.get("max_parallel_model_calls", 4)),
        max_parallel_executions=int(data.get("max_parallel_executions", 4)),
        max_artifact_bytes=int(data.get("max_artifact_bytes", 10 * 1024 * 1024)),
    )


def _decode_delivery(raw: Any) -> DeliverySpec | None:
    if not raw:
        return None
    data = raw if isinstance(raw, dict) else json.loads(raw)
    return DeliverySpec(channel=data.get("channel"), destination=data.get("destination"))


def _opt_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)