# Athena Work Contract

## Purpose

This repository contains Athena's durable agent runtime, governed capability
fabric, interfaces, and native presentation frontend.

## Ownership

The repository owns the normative architecture and the integration contracts
between protocol, reasoning, governance, execution, persistence, and
interfaces. `BUILDSPEC.md`, `SPEC.md`, and the applicable architecture guides
are the normative sources; implementation claims require executable evidence.

## Local Contracts

- There is one reasoning authority: `AgentKernel` decides the next autonomous action.
- `AthenaService` is the application composition root and facade, not a second business-logic layer.
- Capabilities enter through the canonical dispatcher and policy path.
- Execution, persistence, and presentation do not authorize or invent actions.
- Protocol contracts remain implementation-independent and version changes require seam tests.
- Checked-in architecture size budgets are ratchets. Do not widen them to make feature work pass.
- Do not add a second task, session, routing, execution, event-history, or projection authority.

## Work Guidance

- Preserve unrelated dirty work in the checkout; inspect diffs before editing overlapping files.
- Prefer extraction at a real ownership boundary over compatibility wrappers or parallel managers.
- Do not use private cross-package helpers as shared APIs; move shared contracts downward or into a neutral module.
- Keep generated, remote, and model-reachable behavior behind existing governance boundaries.

## Verification

- `make check`
- `cargo build --manifest-path native/Cargo.toml --locked --offline`
- `./scripts/architecture-lint --quiet`
- `cargo check --manifest-path native/Cargo.toml --offline`
- `cargo test --manifest-path native/Cargo.toml --offline`

## Child DOX Index

| Path | Purpose |
|------|---------|
| `src/` | Python source and dependency-direction contracts |
| `native/` | Rust presentation and platform implementation |
| `scripts/` | Architecture, release, and repository tooling |
| `tests/` | Contract, integration, security, crash, and unit evidence |
