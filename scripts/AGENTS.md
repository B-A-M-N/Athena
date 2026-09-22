# Repository Tooling

## Purpose

This subtree contains architecture, release, scenario, and build-support
tools.

## Ownership

Scripts measure and enforce repository contracts. They do not silently rewrite
policy baselines or grant exceptions.

## Local Contracts

- Architecture exceptions require an explicit stable identity, category, and rationale.
- Size baselines are ratchets; widening requires a dedicated waiver and review rationale.
- Import-boundary rules inspect Python's AST so multiline imports cannot bypass them.
- Dependency-cycle baselines are ratchets; new cycles require explicit review.
- Lint rules must report concrete source locations and remain deterministic.
- A tool must not treat a checked-in claim as release evidence without executing the relevant check.

## Work Guidance

Prefer small, reviewable rule additions. Keep policy data separate from scan
logic and add a focused test when a rule changes.

## Verification

- `./scripts/architecture-lint --quiet`
- `make scenarios`
- `make release-check` when release tooling changes

## Child DOX Index

No nested contracts are currently required.
