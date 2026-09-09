"""DeliveryManager: consumes TaskSpec.delivery at finalization (P1-19).

Wired as a task-finalize observer, it runs AFTER the result is durable
(delivery failures never destabilize finalization — the same
failure-isolation contract the knowledge pipeline observer holds). For
each finalized task with a delivery spec it:

1. Resolves the channel's adapter (unknown channel → FAILED receipt,
   never a silent drop).
2. Sends through the adapter (webhook goes through the governed
   external-transaction lifecycle; event_log is already durable).
3. Records the receipt as an event on the task so surfaces can show
   delivery state.
4. Retries retryable failures with bounded attempts and capped backoff.

Idempotence across restarts: a COMPLETED webhook receipt under the same
idempotency identity replays DELIVERED without a second send.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from athena.delivery.adapters import (
    FAILED,
    RETRYABLE,
    DeliveryAdapter,
    DeliveryOutcome,
    EventLogAdapter,
    WebhookAdapter,
)
from athena.protocol.tasks import DeliverySpec, TaskResult, TaskSpec

_logger = logging.getLogger("athena.delivery")

_DEFAULT_MAX_ATTEMPTS = 3
_DEFAULT_BASE_BACKOFF_S = 0.5
_MAX_BACKOFF_S = 8.0


def _destination_identifier(destination: object) -> str | None:
    """Hash a delivery destination before it enters an operator receipt."""
    value = str(destination or "")
    if not value:
        return None
    if value.startswith("destination:"):
        return value
    import hashlib

    return f"destination:{hashlib.sha256(value.encode()).hexdigest()[:24]}"


class DeliveryManager:
    """Finalize-observer that delivers terminal results per TaskSpec.delivery."""

    def __init__(
        self,
        *,
        event_store: Any = None,
        external_store: Any = None,
        adapters: dict[str, DeliveryAdapter] | None = None,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        base_backoff_s: float = _DEFAULT_BASE_BACKOFF_S,
    ) -> None:
        self._max_attempts = max(1, int(max_attempts))
        self._base_backoff_s = max(0.0, float(base_backoff_s))
        self._adapters: dict[str, DeliveryAdapter] = {}
        if event_store is not None:
            self._adapters[EventLogAdapter.channel] = EventLogAdapter(event_store)
        if external_store is not None:
            self._adapters[WebhookAdapter.channel] = WebhookAdapter(external_store)
        if adapters:
            self._adapters.update(adapters)

    def add_adapter(self, adapter: DeliveryAdapter) -> None:
        self._adapters[adapter.channel] = adapter

    # ------------------------------------------------------------------ #
    # Finalize-observer surface (TaskManager.add_finalize_observer)
    # ------------------------------------------------------------------ #
    async def __call__(self, task: TaskSpec, result: TaskResult) -> None:
        spec = getattr(task, "delivery", None)
        if spec is None:
            return
        channel = str(spec.channel or "").strip() or "event_log"
        adapter = self._adapters.get(channel)
        if adapter is None:
            await self._record(
                task,
                result,
                DeliveryOutcome(
                    ok=False,
                    status=FAILED,
                    error=f"no adapter for delivery channel {channel!r}",
                ),
                attempts=1,
            )
            return

        last: DeliveryOutcome | None = None
        attempted_at: str | None = None
        attempts = 0
        for attempt in range(1, self._max_attempts + 1):
            attempts = attempt
            attempted_at = datetime.now(timezone.utc).isoformat()
            outcome = await self._send(adapter, spec, result, task)
            if outcome.ok or not outcome.retryable:
                last = outcome
                break
            last = outcome
            if attempt < self._max_attempts:
                await asyncio.sleep(
                    min(self._base_backoff_s * (2 ** (attempt - 1)), _MAX_BACKOFF_S)
                )
        assert last is not None
        if not last.ok:
            _logger.warning(
                "delivery for task %s via %s failed after %d attempt(s): %s",
                result.task_id,
                channel,
                attempts,
                last.error,
            )
        await self._record(
            task,
            result,
            last,
            attempts=attempts,
            attempted_at=attempted_at,
        )

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #
    async def _send(
        self,
        adapter: DeliveryAdapter,
        spec: DeliverySpec,
        result: TaskResult,
        task: TaskSpec,
    ) -> DeliveryOutcome:
        try:
            return await adapter.send(
                spec,
                result,
                task_id=result.task_id,
                session_id=task.session_id,
                network_policy=getattr(
                    getattr(task, "workspace", None), "network_policy", None
                ),
            )
        except Exception as exc:  # adapter contract violation is still retryable
            _logger.warning(
                "delivery adapter %s raised for task %s: %s",
                adapter.channel,
                result.task_id,
                exc,
            )
            return DeliveryOutcome(ok=False, status=RETRYABLE, error=str(exc))

    async def _record(
        self,
        task: TaskSpec,
        result: TaskResult,
        outcome: DeliveryOutcome,
        *,
        attempts: int,
        attempted_at: str | None = None,
    ) -> None:
        """Emit the delivery receipt as an event; never raise past here."""
        events = None
        event_adapter = self._adapters.get("event_log")
        if isinstance(event_adapter, EventLogAdapter):
            events = event_adapter._events  # noqa: SLF001 - same-package seam
        if events is None:
            return
        receipt = dict(outcome.receipt or {})
        attempted_at = attempted_at or datetime.now(timezone.utc).isoformat()
        receipt.update(
            {
                "delivery_channel": str(getattr(task.delivery, "channel", None) or "event_log"),
                "destination": _destination_identifier(getattr(task.delivery, "destination", None)),
                "delivery_status": outcome.status,
                "ok": outcome.ok,
                "attempts": attempts,
                "attempted_at": attempted_at,
                "result_status": getattr(result.status, "value", None) or str(result.status),
                "summary": result.summary,
            }
        )
        # External transaction receipts can carry the same identity under a
        # provider-specific field. Never let a raw webhook URL leak through
        # the operator-facing delivery event.
        if "external_identity" in receipt:
            receipt["external_identity"] = _destination_identifier(receipt.get("external_identity"))
        if outcome.error:
            receipt["error"] = outcome.error
        try:
            await events.append_event(
                "DeliveryCompleted" if outcome.ok else "DeliveryFailed",
                receipt,
                task_id=result.task_id,
                session_id=task.session_id,
                id=(
                    f"delivery:{result.task_id}:{receipt.get('delivery_channel')}:{receipt.get('delivery_status')}"
                ),
            )
        except Exception as exc:  # noqa: BLE001 - bookkeeping must not propagate
            _logger.warning(
                "delivery receipt for task %s could not be recorded: %s",
                result.task_id,
                exc,
            )


__all__ = ["DeliveryManager"]
