# Fusion

## Purpose

This package coordinates speculative branches, claims, checkpoints, forks, and proof-carrying synthesis.

## Authority And Evidence Boundaries

- The orchestrator coordinates governed mechanisms; it remains subordinate to kernel reasoning and canonical dispatch/policy paths.
- Fusion creates, verifies, and compares candidates; it does not promote them
  into reality. `RealityCoordinator` owns candidate→reality promotion.
- Shadow commits require invariant evidence and must never replay an uncertain partial application as success.
- Branch state is durable and auditable; invalid claims become explicitly stale rather than silently trusted.
- `synthesize_from_branch()` requires an exact verified branch/workspace identity for branch-bound callers, validates against that workspace, and persists branch/workspace/environment provenance; task-local synthesis remains on the canonical dispatcher path.
- The composed acceptance seam covers real Fusion execution followed by RealityCoordinator verification and commit; controlled Fusion-result fixtures are not sufficient evidence for that release gate.
- Parallel comparison is opt-in and bounded to four concurrent isolated candidates; selection, stale-base checks, and reality promotion remain serialized.

## Verification

- Run fusion orchestrator/worldstate tests plus `./scripts/architecture-lint --quiet`.

- `semantic_snapshot.py` owns bounded descriptive checkpoint evidence; `CheckpointManager` remains the file/retention authority and `FusionOrchestrator` only delegates this projection.
- `branch_synthesis.py` owns exact branch-bound generated-capability identity, workspace-fingerprint, environment, provenance, and admission checks; `FusionOrchestrator` only coordinates the operation and does not promote or commit.
- `invariants.py` owns declarative invariant-probe construction and canonical shadow dispatch; the orchestrator decides when the resulting set is checked.
