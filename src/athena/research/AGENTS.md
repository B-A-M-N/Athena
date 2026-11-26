# Research

## Purpose

Acquire, store, filter, and verify evidence under explicit source policy.

## Contract

Research preserves source identity, provenance, freshness, and verification
state. It does not decide task completion or silently promote evidence.

## Verification

Run research, synthesis, and evidence-pipeline tests.

## Child DOX Index

| Path | Purpose |
|------|---------|
| `service.py` | Owns durable research operations and evidence admission. |
| `result_codec.py` | Canonical result, indexing, visibility, and internal payload codecs. |
| `discovery_candidates.py` | Bounded local/provider candidate discovery under source policy. |
| `workflow.py` | Bounded autonomous acquisition and explicit workflow composition. |
| `commands.py` | Typed `ResearchCommand` / `ResearchResult` domain contracts. |
