# Release

## Purpose

This package defines canonical release lanes, qualification, provenance, and native ABI gates.

## Authority And Evidence Boundaries

- Gates evaluate explicit evidence; checked-in claims or prose never substitute for a lane exit code.
- Release identity must bind source SHA, artifacts, toolchain/backend passports, policy version, and signed provenance.
- Failures are fail-closed; absent evidence is not success and no lane may be inferred from another.

## Verification

- Run release gate/provenance tests plus `./scripts/architecture-lint --quiet`.
