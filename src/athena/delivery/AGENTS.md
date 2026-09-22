# Delivery

## Purpose

This package delivers finalized task results through declared channels.

## Authority And Evidence Boundaries

- `DeliveryManager` is a finalize observer, not task completion authority; delivery failure cannot destabilize durable finalization.
- Adapters own channel transport; unknown channels and non-retryable failures produce explicit failed receipts.
- Retries are bounded, destination identifiers are minimized, and webhook sends enter the governed external-transaction path.

## Verification

- Run delivery manager/adapter tests plus `./scripts/architecture-lint --quiet`.
