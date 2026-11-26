# Tests

## Purpose

This subtree provides executable evidence for protocol contracts, subsystem
behavior, security boundaries, crash recovery, and end-to-end seams.

## Ownership

Tests own evidence and fixtures, not production policy. A test should prove
the boundary named by its scope rather than merely exercise an implementation
detail.

## Local Contracts

- Contract tests target public schemas and behavior.
- Security tests prove denied effects do not occur and preserve fail-closed behavior.
- Crash tests exercise kill/restart/replay semantics, not graceful substitutes.
- End-to-end tests exercise real producer/consumer seams such as Python↔Rust projection.
- Deterministic fixtures are preferred; do not mock away the seam under test.

## Work Guidance

Place new evidence in the narrowest matching taxonomy and name the authority
or invariant it proves. Keep unrelated dirty fixtures untouched.

## Verification

- `make test`
- Focused `pytest` invocation for the changed seam
- Native test command for native bridge/render changes

## Child DOX Index

| Path | Purpose |
|------|---------|
| `contract/` | Public contract evidence |
| `security/` | Denial and fail-closed evidence |
| `crash/` | Restart, replay, and recovery evidence |
| `e2e/` | Real subsystem seam evidence |
