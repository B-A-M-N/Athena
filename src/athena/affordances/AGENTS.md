# Affordances

## Purpose

Expose truthful capability visibility and generated-surface overlays.

## Contract

Affordances describe what is available; they do not authorize, execute, or
create an autonomous reasoning loop. Keep inventory revisions and source
provenance explicit.

## Verification

Run affordance tests and `./scripts/architecture-lint --quiet`.

## Child DOX Index

| Path | Contract |
|------|----------|
| `discovery.py` | Reflection tokenization, relevance vocabulary, and dependency fingerprints; subordinate to `CapabilityFabric`. |
| `persistence.py` | Scheduled durable activation tracking and shutdown flushing; subordinate to `CapabilityFabric`. |
| `records.py` | Durable generated-record queries, ownership filters, and startup rehydration. |
| `reflection_ports.py` | Read-only descriptor, availability, prerequisite, and provenance projections; subordinate to `CapabilityFabric`. |
| `search.py` | Read-only capability relevance/ranking projection using fabric-owned descriptor and prerequisite callbacks; no inventory or authorization authority. |
| `overlay_lifecycle.py` | Generated overlay registration, revision validation, activation, supersession, deprecation, and task cleanup through explicit fabric-owned maps/store callbacks. |
