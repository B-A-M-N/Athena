"""Operator projections for durable provider-outcome recovery."""

from __future__ import annotations

from typing import Any, Mapping


async def resolution_view(
    service: Any,
    resolved: Mapping[str, Any],
    contract: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Return a disposition plus its operator-relevant consequences."""
    view = dict(resolved)
    resolution = str(view.get("provider_outcome_status") or "")
    recovery_action = (
        "retry_authorized"
        if resolution == "unknown"
        and view.get("retry_authorized_at")
        and view.get("reservation_released_at") is None
        else resolution
    )
    view["recovery_action"] = recovery_action
    if recovery_action in contract:
        view["disposition_contract"] = dict(contract[recovery_action])
    task_id = str(view.get("task_id") or "")
    tasks = getattr(service, "_store_tasks", None)
    if tasks is not None and task_id:
        row = await tasks.get(task_id)
        view["task_status"] = row.get("status") if row is not None else None
    store = getattr(service, "_model_response_store", None)
    if store is not None and task_id:
        view["liability_open"] = await store.has_unresolved_liability(task_id)
    view["accounted_amount"] = (
        view.get("actual_cost") if resolution == "confirmed_succeeded" else "0"
    )
    view["next_actions"] = (
        ["reconcile original provider outcome or explicitly close provider liability"]
        if resolution == "unknown" and view.get("reservation_released_at") is None
        else ["manually close provider liability"]
        if resolution == "abandoned_with_liability"
        else []
    )
    return view
