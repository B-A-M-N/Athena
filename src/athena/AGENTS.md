# Athena Python Runtime

## Purpose

This subtree implements the application runtime described by the repository
specifications.

## Ownership

Packages own their declared domain mechanisms. Application composition belongs
to `service`; autonomous reasoning belongs to `kernel`; durable truth belongs
to `state`.

## Local Contracts

- A helper may calculate, persist, invoke, validate, or reconcile, but only `kernel` decides the next autonomous action.
- Policy authorizes; execution executes; state records; interfaces translate.
- Neutral cross-cutting contracts must stay below authority packages and must not import an authority to discover implementation behavior.
- Scheduler and research mechanisms must not become alternate agent loops.
- Adapters must converge on the canonical task, capability, event, and result contracts.

## Work Guidance

Use the nearest package contract when editing a scoped subsystem. If a change
creates a durable boundary, add a focused AGENTS.md rather than duplicating
the entire repository contract.

## Verification

Run the nearest package tests plus `./scripts/architecture-lint --quiet` for
authority or dependency changes.

## Child DOX Index

| Path | Purpose |
|------|---------|
| `protocol/` | Implementation-independent contracts |
| `kernel/` | Single reasoning authority |
| `service/` | Composition root and application facade |
| `tasks/` | Task lifecycle authority |
| `policy/` | Authorization and approval decisions |
| `execution/` | Process and runtime execution authority |
| `state/` | Durable persistence and migrations |
| `context/` | Bounded context compilation and provenance |
| `models/` | Model inventory, admission, and routing |
| `capabilities/` | Canonical capability invocation |
| `presentation/` | Interface-neutral projection and bridge contracts |
| `affordances/` | Visibility and overlay inventory |
| `workflows/` | Deterministic capability composition |
| `research/` | Evidence acquisition and verification domain |
| `synthesis/` | Verified generated capabilities and proof-carrying promotion |
| `packs/` | Pack source, install, activation, and hooks |
| `cli/` | Operator-facing command adapter |
| `concurrency/` | Neutral async and keyed-lock utilities |
| `api/` | HTTP/API translation adapter |
| `acp/` | ACP translation adapter |

Cross-cutting modules at this package level (`evidence.py` and `schema.py`) and
the `concurrency/` package provide neutral contracts or utilities shared by
multiple authorities; they are not alternate owners of reasoning, policy, or
execution.
