# Python Source

## Purpose

This subtree contains Athena's Python runtime and all server-side domain
mechanisms.

## Ownership

Python source owns runtime behavior, protocol implementations, persistence,
governance, and interface adapters. Native rendering remains under `native/`.

## Local Contracts

- Dependency direction follows the architecture: protocol contracts are below mechanisms; interfaces depend on service/protocol/presentation contracts.
- `athena.protocol` must not import implementation packages.
- Cross-package private imports are forbidden; private helpers are local implementation details.
- One package may expose an authority, but a refactor must not create a competing authority.
- Model/provider metadata is normalized before admission and ranking.

## Work Guidance

Keep modules bounded by one reason to change. Move shared utilities toward a
neutral lower layer instead of importing across an ownership boundary.

## Verification

- `make lint`
- `make typecheck`
- `make compile`
- `./scripts/architecture-lint --quiet`

## Child DOX Index

| Path | Purpose |
|------|---------|
| `athena/` | Athena Python packages and local authority contracts |
