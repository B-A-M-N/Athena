"""Pure helpers for bounded kernel interpreter observations."""

from __future__ import annotations

from typing import Any

from athena.interpreter.protocol import BodyObservationKind, InterpreterObservation
from athena.interpreter.triggering import REPEATED_FAILURE_THRESHOLD
from athena.protocol.messages import CapabilityResultBlock

__all__ = [
    "budget_exhausted",
    "observation_from_result",
    "repeated_failure_observation",
    "runtime_completed_observation",
]


def observation_from_result(task: Any, result: CapabilityResultBlock):
    error_text = (result.error or "")[:2000]
    output_text = (result.output or "")[:4000]
    if not error_text and not output_text:
        return None
    return InterpreterObservation(
        kind=BodyObservationKind.CAPABILITY_FAILED,
        payload={
            "call_id": result.call_id,
            "capability_id": result.capability_id,
            "error": error_text,
            "output": output_text,
            "generated_failure": dict((result.metadata or {}).get("generated_failure") or {}),
            "diagnostic": dict(
                (result.metadata or {}).get("diagnostic")
                or ((result.metadata or {}).get("generated_failure") or {}).get("diagnostic")
                or {}
            ),
        },
        task_id=task.id,
        session_id=task.session_id,
    )


def repeated_failure_observation(task: Any, result: CapabilityResultBlock, attempts: int):
    if attempts < REPEATED_FAILURE_THRESHOLD:
        return None
    return InterpreterObservation(
        kind=BodyObservationKind.REPEATED_FAILURE,
        payload={
            "capability_id": result.capability_id,
            "attempts": attempts,
            "last_error": (result.error or "")[:500],
        },
        task_id=task.id,
        session_id=task.session_id,
    )


def runtime_completed_observation(task: Any, result: CapabilityResultBlock):
    metadata = result.metadata or {}
    if "exit_code" not in metadata and "resolved_effects" not in metadata:
        return None
    is_execute = result.capability_id in {"execute", "shell", "process"} or (
        "execute" in set(metadata.get("resolved_effects") or ())
    )
    if not is_execute:
        return None
    return InterpreterObservation(
        kind=BodyObservationKind.RUNTIME_COMPLETED,
        payload={
            "call_id": result.call_id,
            "capability_id": result.capability_id,
            "exit_code": metadata.get("exit_code"),
            "timed_out": (result.error or "") == "execution timed out",
            "interrupted": (result.error or "") == "execution interrupted",
            "output_chars": len(result.output or ""),
            "stdout_tail": (result.output or "")[:4000],
            "artifact_uri": getattr(result, "ref_uri", None),
        },
        task_id=task.id,
        session_id=task.session_id,
        artifact_uri=getattr(result, "ref_uri", None),
    )


def budget_exhausted(state: Any, budget: Any) -> bool:
    if budget.max_agent_iterations and state.iterations >= budget.max_agent_iterations:
        return True
    if budget.max_input_tokens is not None and state.input_tokens >= budget.max_input_tokens:
        return True
    if budget.max_output_tokens is not None and state.output_tokens >= budget.max_output_tokens:
        return True
    if budget.max_cost_usd is not None and state.cost >= budget.max_cost_usd:
        return True
    wall_remaining = getattr(state, "budget_wall_time_remaining_s", None)
    if wall_remaining is not None:
        if wall_remaining <= 0:
            return True
    elif budget.max_wall_time is not None and state.elapsed_ms >= int(
        budget.max_wall_time.total_seconds() * 1000
    ):
        return True
    return False
