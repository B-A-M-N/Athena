# Reality

## Purpose

Classify effects and coordinate proven promotion into the real workspace.

## Contract

Reality owns effect disposition and completion proof. It does not select models
or replace the kernel's termination decision.

`candidate_verification.py` is the single candidate proof-planning authority:
project-derived baseline criteria, task criteria, impact analysis, verifier
execution, and retained proof plans. Reality completion and Fusion experiments
both call it rather than defining a second notion of a verified candidate.

Tasks marked complex coding require independent executable proof. If none can
be derived, verification fails closed (`verification_unavailable`) instead of
issuing a synthetic pass.

## Verification

Run reality, dispatcher, shadow, and completion tests.

## Child DOX Index

| Path | Contract |
|------|----------|
| `sensitivity.py` | Static operation/capability vocabulary used by risk classification; subordinate to `RealityGate` and not a predicate authority. |
| `request_risk.py` | Concrete request sensitivity, workspace-binding, and opaque-execution predicates; subordinate to `RealityGate`. |
| `classification_operations.py` | Concrete request-to-facts adaptation for the deterministic classifier; subordinate to `RealityGate`. |
| `transaction_state.py` | Atomic checkpoint-binding persistence for restart reconciliation; subordinate to `RealityGate`. |
| `transaction_operations.py` | Checkpoint compensation, progress, restart reconciliation, and finalization mechanics; subordinate to `RealityGate`. |
| `state.py` | Durable transaction maps and reference-counted route locks; subordinate to `RealityGate`, with no duplicate or legacy gate-state authority. |
| `branch_registry.py` | Active/ephemeral branch maps, restart rehydration, and branch cleanup; subordinate to `RealityGate`. |
| `routing.py` | Immutable route record and deterministic workspace-argument translation; subordinate to `RealityGate`. |
| `routing_operations.py` | Branch and checkpoint route mechanics selected by `RealityGate`; no disposition or policy authority. |
