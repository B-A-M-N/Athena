# Verification

## Purpose

Execute operator-declared acceptance criteria inside a bounded authority
envelope and retain the resulting evidence.

## Local Contracts

- Verifier authority is bounded (observation + bounded execution only).
- A check that cannot run is a failed check, never a pass.
- Verification evidence is durable and immutable once recorded.
- Verifiers never hold secrets, privilege, publication, or computer-input
  authority.

## Verification

Run verification tests and `./scripts/architecture-lint --quiet`.
