# Fusion

## Purpose

This package coordinates speculative branches, claims, checkpoints, forks, and proof-carrying synthesis.

## Authority And Evidence Boundaries

- The orchestrator coordinates governed mechanisms; it remains subordinate to kernel reasoning and canonical dispatch/policy paths.
- Fusion creates, verifies, and compares candidates; it does not promote them
  into reality. `RealityCoordinator` owns candidate→reality promotion.
- Shadow commits require invariant evidence and must never replay an uncertain partial application as success.
- Branch state is durable and auditable; invalid claims become explicitly stale rather than silently trusted.

## Verification

- Run fusion orchestrator/worldstate tests plus `./scripts/architecture-lint --quiet`.
