"""Delivery channel adapters (P1-19).

An adapter turns a terminal :class:`~athena.protocol.tasks.TaskResult`
into one channel-specific send. Two ships today:

* :class:`EventLogAdapter` — local, in-database delivery: append a
  DeliveryCompleted event on the task's session. No network, no external
  transaction; the event store IS the receipt.
* :class:`WebhookAdapter` — HTTP delivery through the governed
  external-transaction lifecycle (the same prepare/begin_apply/finish
  receipt machinery the ``network.http_transaction`` capability uses,
  with the SSRF-guarded request runner).

Adapters are resolved by channel name through :class:`DeliveryManager`.
Every adapter returns a :class:`DeliveryOutcome` and raises nothing past
``send`` — failure shape belongs to the outcome so retry policy can
reason uniformly.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from athena.protocol.capabilities import ExternalEffectPhase
from athena.protocol.ids import new_id
from athena.protocol.tasks import DeliverySpec, TaskResult
from athena.state.external_effects import (
    CONFLICT,
    NEW,
    RECOVERY_REQUIRED,
    REPLAY_COMPLETED,
    SAFE_TO_RETRY,
)


DELIVERED = "DELIVERED"
FAILED = "FAILED"
RETRYABLE = "RETRYABLE"


@dataclass(frozen=True)
class DeliveryOutcome:
    """One delivery attempt's terminal shape."""

    ok: bool
    status: str  # DELIVERED | FAILED | RETRYABLE
    receipt: dict[str, Any] | None = None
    error: str | None = None

    @property
    def retryable(self) -> bool:
        return self.status == RETRYABLE


class DeliveryAdapter(Protocol):
    """One channel's send mechanics. Resolution is by ``channel`` name."""

    channel: str

    async def send(
        self,
        spec: DeliverySpec,
        result: TaskResult,
        *,
        task_id: str,
        session_id: str | None = None,
    ) -> DeliveryOutcome: ...


def _status_value(result: TaskResult) -> str:
    return getattr(result.status, "value", None) or str(result.status)


# --------------------------------------------------------------------------- #
# Local event-log channel
# --------------------------------------------------------------------------- #
class EventLogAdapter:
    """Deliver into the canonical event stream on the task's session.

    This is the default channel (``delivery.channel = "event_log"``, and
    the fallback when a spec names nothing): the durable, already-audited
    surface every consumer already watches. Delivering here IS recording
    the receipt — :meth:`send` is a success marker and the manager's
    ``_record`` appends the single DeliveryCompleted/DeliveryFailed event
    that carries the outcome.
    """

    channel = "event_log"

    def __init__(self, event_store: Any) -> None:
        self._events = event_store

    async def send(
        self,
        spec: DeliverySpec,
        result: TaskResult,
        *,
        task_id: str,
        session_id: str | None = None,
    ) -> DeliveryOutcome:
        # The event append itself happens once, in DeliveryManager._record;
        # no second artifact for this channel.
        return DeliveryOutcome(ok=True, status=DELIVERED, receipt={"channel": self.channel})


# --------------------------------------------------------------------------- #
# HTTP webhook channel
# --------------------------------------------------------------------------- #
def _delivery_digest(spec: DeliverySpec, result: TaskResult) -> str:
    body = json.dumps(
        {
            "channel": spec.channel,
            "destination": spec.destination,
            "task_id": result.task_id,
            "status": _status_value(result),
            "summary": result.summary,
        },
        sort_keys=True,
        default=str,
    ).encode()
    return hashlib.sha256(body).hexdigest()


def _destination_identifier(destination: str) -> str:
    """Return a stable destination id without persisting URL credentials/query data."""
    return f"destination:{hashlib.sha256(destination.encode()).hexdigest()[:24]}"


class WebhookAdapter:
    """Deliver to an HTTP endpoint through the external-transaction lifecycle.

    Uses the same durable receipt machinery as ``network.http_transaction``:
    ``prepare`` write-aheads the transaction, ``begin_apply`` fences unknown
    outcomes (a crash mid-send becomes recovery-required, never a silent
    duplicate), ``finish`` records the terminal receipt. The Idempotency-Key
    header makes honest receivers duplicate-safe too.
    """

    channel = "webhook"

    def __init__(
        self,
        external_store: Any,
        http_runner: Callable[..., Any] | None = None,
    ) -> None:
        self._external_store = external_store
        # Injectable for tests; production resolves lazily so the
        # environment module's httpx import stays out of import time.
        self._http_runner = http_runner

    async def send(
        self,
        spec: DeliverySpec,
        result: TaskResult,
        *,
        task_id: str,
        session_id: str | None = None,
    ) -> DeliveryOutcome:
        if not str(spec.destination or ""):
            return DeliveryOutcome(
                ok=False, status=FAILED, error="webhook delivery requires a destination URL"
            )
        from athena.capabilities.environment import _run_external_http_request

        destination = str(spec.destination)
        runner = self._http_runner or _run_external_http_request
        capability_id = f"delivery.{self.channel}"
        request_digest = _delivery_digest(spec, result)
        idempotency_key = f"delivery:{result.task_id}:{request_digest[:24]}"

        external_identity = _destination_identifier(destination)
        transaction_id = new_id("delivery-tx")
        try:
            prepare_or_recover = getattr(self._external_store, "prepare_or_recover", None)
            if prepare_or_recover is not None:
                preparation = await prepare_or_recover(
                    transaction_id=transaction_id,
                    task_id=task_id,
                    capability_id=capability_id,
                    external_identity=external_identity,
                    request_digest=request_digest,
                    idempotency_key=idempotency_key,
                )
                preparation_status = str(preparation.status)
                receipt = dict(preparation.receipt)
                if preparation_status == REPLAY_COMPLETED:
                    return DeliveryOutcome(ok=True, status=DELIVERED, receipt=receipt)
                if preparation_status in {CONFLICT, RECOVERY_REQUIRED}:
                    reason = (
                        "delivery idempotency key conflicts with another request"
                        if preparation_status == CONFLICT
                        else "delivery outcome is unknown; explicit recovery is required"
                    )
                    return DeliveryOutcome(ok=False, status=FAILED, error=reason, receipt=receipt)
                if preparation_status not in {NEW, SAFE_TO_RETRY}:
                    return DeliveryOutcome(
                        ok=False,
                        status=FAILED,
                        error=f"unsupported external preparation state: {preparation_status}",
                        receipt=receipt,
                    )
                transaction_id = str(receipt["transaction_id"])
            else:
                # Compatibility for small third-party test doubles. The
                # production store always implements prepare_or_recover.
                await self._external_store.prepare(
                    transaction_id=transaction_id,
                    task_id=task_id,
                    capability_id=capability_id,
                    external_identity=external_identity,
                    request_digest=request_digest,
                    idempotency_key=idempotency_key,
                    phase=ExternalEffectPhase.PREPARE,
                )
                await self._external_store.finish(
                    transaction_id,
                    status="PREPARED",
                    phase=ExternalEffectPhase.PREPARE,
                )
            receipt, replay = await self._external_store.begin_apply(
                transaction_id=transaction_id,
                task_id=task_id,
                capability_id=capability_id,
                external_identity=external_identity,
                request_digest=request_digest,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:  # noqa: BLE001 - receipt store failure is ours
            return DeliveryOutcome(
                ok=False, status=FAILED, error=f"delivery transaction prepare failed: {exc}"
            )

        if replay:
            # A previous process already delivered these bytes (COMPLETED
            # receipt under the same idempotency identity). Do not re-send.
            return DeliveryOutcome(ok=True, status=DELIVERED, receipt=dict(receipt))

        body = json.dumps(
            {
                "task_id": result.task_id,
                "status": _status_value(result),
                "summary": result.summary,
            },
            default=str,
        )
        try:
            response = runner(
                url=destination,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Idempotency-Key": idempotency_key,
                },
                body=body,
                timeout=10.0,
                follow_redirects=False,
                policy_name=None,
            )
            if inspect.isawaitable(response):
                response = await response
        except Exception as exc:  # noqa: BLE001 - remote outcome is uncertain
            try:
                receipt = await self._external_store.finish(
                    transaction_id, status="RECOVERY_REQUIRED", error=str(exc)
                )
            except Exception:
                receipt = {"transaction_id": transaction_id, "error": str(exc)}
            return DeliveryOutcome(ok=False, status=FAILED, error=str(exc), receipt=receipt)

        if not isinstance(response, Mapping):
            # A runner that resolves to a non-mapping breaks the response
            # contract; fail closed into recovery rather than raise mid-run.
            try:
                receipt = await self._external_store.finish(
                    transaction_id,
                    status="RECOVERY_REQUIRED",
                    error=f"delivery runner returned {type(response).__name__}, expected mapping",
                )
            except Exception:
                receipt = {
                    "transaction_id": transaction_id,
                    "error": "delivery runner returned a non-mapping response",
                }
            return DeliveryOutcome(
                ok=False,
                status=FAILED,
                error="delivery runner returned a non-mapping response",
                receipt=receipt,
            )

        status_code = int(response.get("status") or 0)
        if 200 <= status_code < 300:
            receipt = await self._external_store.finish(
                transaction_id,
                status="COMPLETED",
                response={"http_status": status_code},
            )
            return DeliveryOutcome(ok=True, status=DELIVERED, receipt=receipt)
        if 400 <= status_code < 500:
            # The receiver rejected these bytes definitively; retrying the
            # same request cannot help.
            receipt = await self._external_store.finish(
                transaction_id,
                status="APPLY_REJECTED",
                response={"http_status": status_code},
                error=f"delivery endpoint returned {status_code}",
            )
            return DeliveryOutcome(
                ok=False, status=FAILED, error=f"endpoint returned {status_code}", receipt=receipt
            )
        receipt = await self._external_store.finish(
            transaction_id,
            status="RECOVERY_REQUIRED",
            response={"http_status": status_code},
            error=f"delivery endpoint returned {status_code}; remote outcome requires verification",
        )
        return DeliveryOutcome(
            ok=False,
            status=FAILED,
            error=f"endpoint returned {status_code}; remote outcome requires verification",
            receipt=receipt,
        )


__all__ = [
    "DeliveryAdapter",
    "DeliveryOutcome",
    "EventLogAdapter",
    "WebhookAdapter",
    "DELIVERED",
    "FAILED",
    "RETRYABLE",
]
