# Project

## Purpose

This package owns project profiles and the service-facing project index boundary.

## Authority And Evidence Boundaries

- `ProjectIndexCoordinator` owns per-workspace cache freshness and invalidation; builders only produce an index from source.
- Completion-critical consumers must request source-verified freshness when stale external edits can invalidate proof.
- Index observations do not execute work or become task completion evidence by themselves.
- `index/cache.py` owns the bounded in-memory LRU window; durable index state remains in the store.

## Verification

- Run project index/profile tests plus `./scripts/architecture-lint --quiet`.
