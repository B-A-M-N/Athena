# Durable State

## Purpose

State owns durable repositories, transactions, event history, and persistence
truth.

## Ownership

State records and retrieves facts. It does not make product decisions, reason,
authorize, invoke providers, or execute processes.

## Local Contracts

- Database access stays inside state repositories.
- Transactions and event ordering must remain explicit and crash-safe.
- A stable event ID is an idempotency key. Exact canonical replay is dropped;
  any semantic identity difference is a typed conflict, never silent reuse.
- Shared async/blocking helpers belong below state, not in execution-specific modules.
- Schema changes require forward/backward compatibility review and migration evidence.

## Work Guidance

Keep repositories narrow and use protocol codecs rather than importing kernel
or interface internals.

## Verification

Run state, task, migration, and crash/recovery tests.

## Child DOX Index

| Path | Purpose |
|------|---------|
| `migrations/` | Crash-safe schema evolution |
| `context_digests.py` | Persist and retrieve neutral context-digest records |
| `model_response_reconciliation.py` | Legacy inference-receipt migration guards and provider-cost normalization subordinate to `ModelResponseStore`. |
