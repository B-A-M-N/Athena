"""Pure replay-policy and runtime-budget helpers.

Owned by neither the kernel nor the continuation coordinator. Extracted
from kernel.py so continuations_coordinator.py no longer needs a dynamic
owner-backimport (review item 15).
"""

from __future__ import annotations


from athena.protocol.messages import CapabilityResultBlock, utcnow
from athena.protocol.tasks import TaskSpec


def replay_policy_context(task) -> dict:
    """Canonical extraction of the authority context approval replay needs.

    Replay paths must restore the SAME authority the original dispatch ran
    under: model policy, capability policy, task budget, deadline. Fields
    absent from a partial test double default to permissive-None, which the
    dispatcher treats as unset — never as a widened grant.
    """
    return {
        "task_policy": getattr(task, "capability_policy", None),
        "model_policy": getattr(task, "model_policy", None),
        "task_budget": getattr(task, "resource_budget", None),
        "task_deadline": getattr(task, "deadline", None),
    }


def remaining_runtime_seconds(task: TaskSpec, state) -> float | None:
    """Return the bounded wall-clock remaining, or None when unbounded."""
    limits: list[float] = []
    deadline = getattr(task, "deadline", None)
    if deadline is not None:
        limits.append((deadline - utcnow()).total_seconds())
    budget = getattr(task, "resource_budget", None)
    wall_remaining = getattr(state, "budget_wall_time_remaining_s", None)
    if wall_remaining is not None:
        elapsed_since_checkpoint = max(
            0.0,
            state.elapsed_ms / 1000 - getattr(state, "budget_wall_time_checkpoint_s", 0.0),
        )
        limits.append(wall_remaining - elapsed_since_checkpoint)
    else:
        max_wall_time = getattr(budget, "max_wall_time", None)
        if max_wall_time is not None:
            limits.append(max_wall_time.total_seconds() - state.elapsed_ms / 1000)
    return min(limits) if limits else None


def deny_result(suspended) -> CapabilityResultBlock:
    """Canonical denied-result block for a suspended call."""
    call_id = getattr(suspended, "call_id", "")
    req = getattr(suspended, "request", None)
    capability_id = getattr(req, "capability_id", "") if req is not None else ""
    return CapabilityResultBlock(
        call_id=call_id,
        capability_id=capability_id,
        ok=False,
        error="denied: approval not granted",
    )
