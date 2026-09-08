"""Terminal-result delivery (P1-19).

``TaskSpec.delivery`` names a channel and destination, but until now no
subsystem consumed it — a completed task's result stayed local even when
its spec asked for delivery.

This package closes that loop: :class:`~athena.delivery.manager.DeliveryManager`
is a task-finalization observer that resolves the adapter for the spec's
channel, applies the task's own external-effect policy, sends through the
governed external-transaction lifecycle (prepare → apply → finish receipts,
idempotency-keyed), records the receipt, and retries bounded, retryable
failures. Delivery is an external publish (EffectClass.EXTERNAL_PUBLISH):
it is always receipted, never fire-and-forget.
"""

from __future__ import annotations

from athena.delivery.adapters import (
    DeliveryAdapter,
    DeliveryOutcome,
    EventLogAdapter,
    WebhookAdapter,
)
from athena.delivery.manager import DeliveryManager

__all__ = [
    "DeliveryAdapter",
    "DeliveryManager",
    "DeliveryOutcome",
    "EventLogAdapter",
    "WebhookAdapter",
]
