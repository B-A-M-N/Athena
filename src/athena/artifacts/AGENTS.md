# Artifacts

## Purpose

This package owns immutable, content-addressed artifact storage and provenance references.

## Authority And Evidence Boundaries

- `ArtifactStore` owns blob identity, metadata sidecars, bounded I/O, retention, and budget accounting.
- Artifact truth is filesystem-backed and immutable; callers may reference evidence, never rewrite stored payloads.
- Producers must attach real provenance; extraction may derive references but must not fabricate completion proof.

## Verification

- Run focused artifact store/reference tests plus `./scripts/architecture-lint --quiet`.
