# Presentation Contract

## Purpose

This package owns the interface-neutral projection from canonical Athena
events to bounded presentation facts, plus the serialized seam consumed by
native and hosted surfaces.

## Ownership

- `ProjectionState` is the one event-to-view reducer.
- `schema.py` owns the versioned Python bridge contract and structured failure
  envelope.
- `native_bridge.py` serializes projection facts; it does not execute,
  authorize, infer, or repair runtime state.
- Renderers may choose layout and medium, but they must not create a second
  projection or reinterpret backend truth.

## Local Contracts

- Canonical event payloads remain authoritative; presentation only bounds,
  sanitizes, and organizes observed facts.
- Native consumes the DTO read-only and reports malformed/unsupported frames
  as structured bridge failures.
- Compatibility imports under `athena.cli` may remain temporarily, but new
  shared consumers import from this package.

## Child DOX Index

| Path | Purpose |
|------|---------|
| `projection.py` | Canonical event-to-view reducer |
| `execution_projection.py` | Execution/runtime/mutation/verification event reducer; subordinate to `ProjectionState`. |
| `schema.py` | Versioned native frame and failure contract |
| `native_bridge.py` | Python serializer for the native projection DTO |
| `semantics.py` | Shared visual action vocabulary and event classification |
| `scene.py` | Normalized OI scene and runtime/workspace trees |
| `ansi_scene.py` | ANSI medium adapter for the shared scene |
| `ansi.py` | Cell-aware ANSI grid primitive |
| `layout.py` | Interface-neutral chassis geometry |
| `code_view.py` | Bounded code and diff view facts |
| `text.py` | Terminal-safe presentation text utility |
