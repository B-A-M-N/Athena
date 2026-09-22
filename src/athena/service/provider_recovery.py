"""Durable provider-outcome reconciliation service."""

from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation
from typing import Any

from athena.protocol.failure import FailureInfo
from athena.protocol.tasks import FINAL_STATUSES, TaskStatus
from athena.service.provider_recovery_ports import ProviderRecoveryPorts


def _validated_actual_cost(value: Decimal | str | None) -> Decimal | None:
    """Validate the operator/API cost at the recovery boundary."""
    if value is None:
        return None
    try:
        cost = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("actual_cost must be a finite, non-negative Decimal") from exc
    if not cost.is_finite() or cost < 0:
        raise ValueError("actual_cost must be a finite, non-negative Decimal")
    return cost


PROVIDER_OUTCOME_CONTRACT: dict[str, dict[str, Any]] = {
    "confirmed_failed": {
        "attempt_status": "FAILED",
        "receipt_status": "FAILED",
        "reservation": "released",
        "actual_cost": "not_required",
        "task_transition": "RECOVERY_REQUIRED -> FAILED",
        "inference_retry": "no",
        "operator_revisable": False,
    },
    "retry_authorized": {
        "attempt_status": "UNKNOWN",
        "receipt_status": "FAILED",
        "reservation": "released",
        "actual_cost": "not_required",
        "task_transition": "RECOVERY_REQUIRED|INTERRUPTED -> RUNNING",
        "inference_retry": "one replacement attempt",
        "operator_revisable": False,
    },
    "confirmed_succeeded": {
        "attempt_status": "COMPLETED",
        "receipt_status": "FAILED",
        "reservation": "released after actual-cost accounting",
        "actual_cost": "required finite non-negative Decimal",
        "task_transition": "RECOVERY_REQUIRED -> FAILED",
        "inference_retry": "no",
        "operator_revisable": False,
    },
    "abandoned_with_liability": {
        "attempt_status": "ABANDONED",
        "receipt_status": "FAILED",
        "reservation": "retained until manual liability closeout",
        "actual_cost": "optional/unknown",
        "task_transition": "RECOVERY_REQUIRED -> FAILED",
        "inference_retry": "no",
        "operator_revisable": False,
    },
}


class ProviderOutcomeRecoveryAPI:
    @staticmethod
    def compose(owner: Any) -> "ProviderOutcomeRecoveryService":
        return ProviderOutcomeRecoveryService(ProviderRecoveryPorts(owner))

    def _provider_recovery_service(self) -> "ProviderOutcomeRecoveryService":
        recovery = getattr(self, "__dict__", {}).get("_provider_recovery_runtime")
        if recovery is None:
            raise RuntimeError("provider outcome recovery is not composed")
        return recovery

    async def list_provider_outcome_recoveries(
        self, task_id: str | None = None
    ) -> list[dict[str, Any]]:
        return await ProviderOutcomeRecoveryAPI._provider_recovery_service(self).list_unresolved(
            task_id=task_id
        )

    async def get_provider_outcome_recovery(self, attempt_id: str) -> dict[str, Any] | None:
        return await ProviderOutcomeRecoveryAPI._provider_recovery_service(self).get(attempt_id)

    async def resolve_provider_outcome(
        self,
        attempt_id: str,
        *,
        resolution: str,
        note: str,
        provider_response_id: str | None = None,
        actual_cost: Decimal | str | None = None,
    ) -> dict[str, Any]:
        return await ProviderOutcomeRecoveryAPI._provider_recovery_service(self).resolve(
            attempt_id,
            resolution=resolution,
            note=note,
            provider_response_id=provider_response_id,
            actual_cost=actual_cost,
        )

    async def close_provider_liability(self, attempt_id: str, *, note: str) -> dict[str, Any]:
        return await ProviderOutcomeRecoveryAPI._provider_recovery_service(self).close_liability(
            attempt_id, note=note
        )

    async def reconcile_provider_outcomes(self) -> dict[str, int]:
        return await ProviderOutcomeRecoveryAPI._provider_recovery_service(self).reconcile()


class ProviderOutcomeRecoveryService:
    def __init__(self, ports: ProviderRecoveryPorts) -> None:
        self._ports = ports

    def _store(self):
        store = self._ports.response_store()
        if store is None:
            raise RuntimeError("provider outcome recovery store is unavailable")
        return store

    async def list_unresolved(self, task_id: str | None = None) -> list[dict[str, Any]]:
        store = self._store()
        try:
            rows = await store.list_unresolved_attempts(task_id=task_id)
        except Exception as exc:
            health = self._ports.health()
            if health is not None:
                health.update({"state": "unavailable", "error": str(exc)})
            raise RuntimeError("provider outcome recovery store could not be read") from exc
        health = self._ports.health()
        if health is not None:
            health.update(
                {
                    "state": "degraded" if rows else "ready",
                    "unresolved_count": len(rows),
                    "error": None,
                }
            )
        return rows

    async def get(self, attempt_id: str) -> dict[str, Any] | None:
        return await self._store().get_attempt(str(attempt_id))

    async def resolve(
        self,
        attempt_id: str,
        *,
        resolution: str,
        note: str,
        provider_response_id: str | None = None,
        actual_cost: Decimal | str | None = None,
    ) -> dict[str, Any]:
        resolution = str(resolution).strip().lower()
        note = str(note).strip()
        if not note:
            raise ValueError("provider outcome resolution requires a non-empty note")
        normalized_cost = _validated_actual_cost(actual_cost)
        store = self._store()
        attempt = await store.get_attempt(str(attempt_id))
        if attempt is None:
            raise KeyError(f"unknown inference attempt: {attempt_id}")
        if resolution == "confirmed_succeeded" and normalized_cost is None:
            prior_cost = attempt.get("actual_cost")
            if prior_cost in (None, ""):
                raise ValueError("confirmed_succeeded requires actual_cost")
            normalized_cost = _validated_actual_cost(str(prior_cost))
        resolved = await store.resolve_provider_outcome(
            attempt_id=str(attempt_id),
            resolution=resolution,
            note=note,
            provider_response_id=provider_response_id,
            actual_cost=normalized_cost,
        )
        task_id = str(resolved.get("task_id") or attempt.get("task_id") or "")
        amount = _validated_actual_cost(resolved.get("reservation_amount")) or Decimal("0")
        if resolution in {"confirmed_failed", "retry_authorized"}:
            await self._release_provider_reservation(
                store, task_id=task_id, attempt_id=str(attempt_id), amount=amount
            )
        elif resolution == "confirmed_succeeded":
            budgets = self._ports.budgets()
            if budgets is not None:
                await budgets.apply_model_accounting(
                    task_id,
                    str(attempt_id),
                    reserved=amount,
                    input_tokens=int(resolved.get("actual_input_tokens") or 0),
                    output_tokens=int(resolved.get("actual_output_tokens") or 0),
                    actual_cost=normalized_cost,
                    reservation_id=str(attempt_id),
                )
                await store.mark_budget_accounted(attempt_id=str(attempt_id))
            await store.mark_reservation_released(attempt_id=str(attempt_id))
        events = self._ports.events()
        if events is not None:
            await events.append_event(
                "ProviderOutcomeResolved",
                {
                    "attempt_id": str(attempt_id),
                    "task_id": task_id,
                    "resolution": resolution,
                    "provider": resolved.get("provider"),
                    "model": resolved.get("model"),
                    "provider_response_id": resolved.get("provider_response_id"),
                },
                task_id=task_id or None,
            )
        if resolution == "retry_authorized":
            await self._launch_provider_retry(task_id)
        elif resolution in {
            "confirmed_failed",
            "confirmed_succeeded",
            "abandoned_with_liability",
        }:
            await self._finalize_provider_outcome(
                task_id, resolution=resolution, attempt_id=str(attempt_id)
            )
        return await self._provider_resolution_view(resolved)

    async def close_liability(self, attempt_id: str, *, note: str) -> dict[str, Any]:
        note = str(note).strip()
        if not note:
            raise ValueError("provider liability closeout requires a non-empty note")
        store = self._store()
        attempt = await store.get_attempt(str(attempt_id))
        if attempt is None:
            raise KeyError(f"unknown inference attempt: {attempt_id}")
        if str(attempt.get("provider_outcome_status") or "").lower() != (
            "abandoned_with_liability"
        ):
            raise ValueError("only abandoned provider liabilities can be manually closed")
        amount = _validated_actual_cost(attempt.get("reservation_amount")) or Decimal("0")
        budgets = self._ports.budgets()
        if budgets is not None and amount > 0:
            await budgets.release_model_cost(
                str(attempt.get("task_id") or ""), amount, reservation_id=str(attempt_id)
            )
        closed = await store.close_provider_liability(attempt_id=str(attempt_id), note=note)
        events = self._ports.events()
        if events is not None:
            await events.append_event(
                "ProviderLiabilityClosed",
                {
                    "attempt_id": str(attempt_id),
                    "task_id": closed.get("task_id"),
                    "note": note,
                },
                task_id=str(closed.get("task_id") or "") or None,
            )
        view = await self._provider_resolution_view(closed)
        view["liability_closed"] = True
        view["next_actions"] = []
        return view

    async def _release_provider_reservation(
        self, store: Any, *, task_id: str, attempt_id: str, amount: Decimal
    ) -> None:
        budgets = self._ports.budgets()
        if budgets is not None and amount > 0:
            await budgets.release_model_cost(task_id, amount, reservation_id=attempt_id)
        await store.mark_reservation_released(attempt_id=attempt_id)

    async def _finalize_provider_outcome(
        self, task_id: str, *, resolution: str, attempt_id: str
    ) -> None:
        manager = self._ports.task_manager()
        tasks = self._ports.tasks()
        if manager is None or tasks is None or not task_id:
            return
        row = await tasks.get(task_id)
        if row is None:
            return
        current = TaskStatus(str(row.get("status") or ""))
        if current in FINAL_STATUSES:
            return
        if current is not TaskStatus.RECOVERY_REQUIRED:
            await manager.transition(
                task_id,
                TaskStatus.RECOVERY_REQUIRED,
                reason=f"provider outcome {resolution} requires task finalization",
            )
        unresolved = (
            (f"provider_response_unavailable:{attempt_id}",)
            if resolution == "confirmed_succeeded"
            else (f"provider_liability:{attempt_id}",)
            if resolution == "abandoned_with_liability"
            else ()
        )
        await manager.finalize(
            task_id,
            status=TaskStatus.FAILED,
            reason=f"provider outcome resolved as {resolution}",
            summary=f"Provider outcome resolved as {resolution}; no safe continuation remains.",
            unresolved=unresolved,
            failure=FailureInfo(
                message=f"provider outcome resolved as {resolution}",
                code="provider_outcome_recovery",
                stage="model_provider_recovery",
                kind="model_provider",
                fatal=True,
            ),
            _allow_recovery_completion=True,
        )

    async def _launch_provider_retry(self, task_id: str) -> None:
        manager = self._ports.task_manager()
        tasks = self._ports.tasks()
        kernel = self._ports.kernel()
        if manager is None or tasks is None or kernel is None or not task_id:
            return
        row = await tasks.get(task_id)
        if row is None or str(row.get("status") or "") in {item.value for item in FINAL_STATUSES}:
            return
        status = TaskStatus(str(row.get("status") or ""))
        if status in {TaskStatus.RECOVERY_REQUIRED, TaskStatus.INTERRUPTED}:
            await manager.transition(
                task_id,
                TaskStatus.RUNNING,
                reason="provider outcome reconciled; retry authorized",
            )
        elif status is not TaskStatus.RUNNING:
            return
        recovery = asyncio.create_task(kernel.run_task(task_id))
        registry = self._ports.approval_recovery_tasks()
        if registry is not None:
            registry.add(recovery)
        log_failure = self._ports.log_background_failure()
        recovery.add_done_callback(log_failure(f"provider outcome recovery {task_id}"))

    async def _provider_resolution_view(self, resolved: dict[str, Any]) -> dict[str, Any]:
        view = dict(resolved)
        resolution = str(view.get("provider_outcome_status") or "")
        if resolution in PROVIDER_OUTCOME_CONTRACT:
            view["disposition_contract"] = dict(PROVIDER_OUTCOME_CONTRACT[resolution])
        task_id = str(view.get("task_id") or "")
        tasks = self._ports.tasks()
        if tasks is not None and task_id:
            row = await tasks.get(task_id)
            view["task_status"] = row.get("status") if row is not None else None
        store = self._ports.response_store()
        if store is not None and task_id:
            view["liability_open"] = await store.has_unresolved_liability(task_id)
        view["accounted_amount"] = (
            view.get("actual_cost") if resolution == "confirmed_succeeded" else "0"
        )
        view["next_actions"] = (
            ["manually close provider liability"]
            if resolution == "abandoned_with_liability"
            else []
        )
        return view

    async def reconcile(self) -> dict[str, int]:
        store = self._store()
        replayed = 0
        failed = 0
        for attempt in await store.list_resolved_attempts():
            try:
                resolution = str(attempt.get("provider_outcome_status") or "")
                task_id = str(attempt.get("task_id") or "")
                amount = _validated_actual_cost(attempt.get("reservation_amount")) or Decimal("0")
                attempt_id = str(attempt.get("attempt_id") or "")
                receipt = await store.get_receipt(
                    task_id=task_id,
                    request_fingerprint=str(attempt.get("request_fingerprint") or ""),
                )
                if (
                    resolution == "retry_authorized"
                    and receipt is not None
                    and str(receipt.get("attempt_id") or "") != attempt_id
                ):
                    replayed += 1
                    continue
                if resolution in {"confirmed_failed", "retry_authorized"} and not attempt.get(
                    "reservation_released_at"
                ):
                    await self._release_provider_reservation(
                        store, task_id=task_id, attempt_id=attempt_id, amount=amount
                    )
                elif resolution == "confirmed_succeeded":
                    cost = _validated_actual_cost(attempt.get("actual_cost"))
                    if cost is None:
                        raise RuntimeError(f"resolved success {attempt_id} has no actual cost")
                    budgets = self._ports.budgets()
                    if not attempt.get("budget_accounted_at") and budgets is not None:
                        await budgets.apply_model_accounting(
                            task_id,
                            attempt_id,
                            reserved=amount,
                            input_tokens=int(attempt.get("actual_input_tokens") or 0),
                            output_tokens=int(attempt.get("actual_output_tokens") or 0),
                            actual_cost=cost,
                            reservation_id=attempt_id,
                        )
                        await store.mark_budget_accounted(attempt_id=attempt_id)
                    if not attempt.get("reservation_released_at"):
                        await store.mark_reservation_released(attempt_id=attempt_id)
                if resolution == "retry_authorized":
                    await self._launch_provider_retry(task_id)
                elif resolution in {
                    "confirmed_failed",
                    "confirmed_succeeded",
                    "abandoned_with_liability",
                }:
                    await self._finalize_provider_outcome(
                        task_id, resolution=resolution, attempt_id=attempt_id
                    )
                replayed += 1
            except Exception:
                failed += 1
                raise
        return {"replayed": replayed, "failed": failed}


__all__ = [
    "PROVIDER_OUTCOME_CONTRACT",
    "ProviderOutcomeRecoveryAPI",
    "ProviderOutcomeRecoveryService",
]
