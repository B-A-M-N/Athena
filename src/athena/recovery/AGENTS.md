# Recovery

## Purpose

Recover interrupted work with durable evidence; never guess terminal state.

## Local Contracts

- Recovery retains failure evidence; it never fabricates success.
- Interrupted tasks park as INTERRUPTED (resumable), never silently CANCELLED.
- Every recovery outcome is durably recorded before it is acted on.
- Unverifiable state is surfaced as recovery-required, not assumed.

## Verification

Run recovery/crash tests and `./scripts/architecture-lint --quiet`.
