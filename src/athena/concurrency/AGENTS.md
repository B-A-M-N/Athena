# Concurrency

## Purpose

Provide neutral async primitives shared by persistence, reasoning, execution,
and interfaces.

## Local Contracts

- This package owns synchronization mechanics only; it never authorizes work,
  executes capabilities, or owns task/session state.
- `ReferenceCountedKeyedLocks` is the shared keyed-lock boundary. Long-lived
  owners must scope lock acquisition and release references deterministically.
- Do not reintroduce package-private keyed-lock helpers across authority
  boundaries.

## Verification

- `pytest tests/unit/test_concurrency.py -q`
- `pytest tests/performance/test_keyed_state_retention.py -q`
- `./scripts/architecture-lint --quiet`
