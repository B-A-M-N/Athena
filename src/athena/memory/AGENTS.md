# Memory

## Purpose

This package owns durable memory records, evidence-linked candidates, conflict state, embeddings, and retrieval.

## Authority And Evidence Boundaries

- `MemoryStore` owns record persistence, trust/scope metadata, invalidation, and retrieval normalization.
- Semantic retrieval requires the configured embedding provider; unavailable capabilities must be explicit, never silently lexical.
- Candidate promotion requires durable provenance and evidence; contradictions remain visible rather than overwritten.

## Child DOX Index

| Path | Contract |
|------|----------|
| `candidate_lifecycle.py` | Durable pending-candidate list, promotion, discard, expiry, and bounded compaction; subordinate to `MemoryStore`, never trust or retrieval authority. |
| `embedding_index.py` | Derived vector indexing/backfill and nearest-neighbor mechanics; subordinate to `MemoryStore`, never canonical memory authority. |
| `retrieval_store.py` | Scope predicates and SQLite FTS/recency queries; subordinate to `MemoryStore`, never record or trust authority. |
| `record_codec.py` | Canonical memory-row and namespaced metadata serialization; subordinate to `MemoryStore`, never persistence or trust authority. |
| `queries.py` | Read-only memory-row projections by identifier, scope, kind, and count; subordinate to `MemoryStore`, never mutation or trust authority. |
| `writes.py` | Conflict-aware memory-row insertion and trust admission through explicit store callbacks; subordinate to `MemoryStore`, never a second memory authority. |

## Verification

- Run memory store/retrieval/candidate tests plus `./scripts/architecture-lint --quiet`.
