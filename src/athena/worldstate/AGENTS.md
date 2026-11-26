# World State

## Purpose

This package owns execution-grounded claims, invariants, and structured task reality.

## Authority And Evidence Boundaries

- Claims persist only with executable evidence and become stale when dependent workspace truth changes.
- `InvariantSet` is a pre-commit gate for speculative branches, not a per-mutation runtime policy owner.
- Stores observe and persist state; they never execute work or invent successful claims.

## Verification

- Run worldstate claim/invariant/store tests plus `./scripts/architecture-lint --quiet`.
