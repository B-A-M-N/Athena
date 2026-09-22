# CLI Adapter Contract

## Purpose

This package owns operator input, lifecycle, terminal transport, and surface
adapters. Shared projection semantics belong to `athena.presentation`.

## Local Contracts

- CLI surfaces consume `ProjectionState`; they do not reduce events into a
  second state model.
- `projection.py` and `native_bridge.py` remain compatibility import paths for
  existing integrations; new shared code should import from
  `athena.presentation`.
- Terminal lifecycle and physical rendering may adapt presentation facts, but
  they must not authorize or execute work.
- `framebuffer_cache.py` owns Pillow font and retained-layer cache lifetime;
  `framebuffer.py` owns scene drawing and animation composition.
- `framebuffer_layout.py` owns deterministic Buddy collision/placement geometry;
  it does not render pixels or own framebuffer cache state.
- `framebuffer_world.py` owns bounded animated Buddy terrain and dot-matrix
  owl pixels; it does not own scene composition, cache lifetime, or placement.
- `event_projection.py` owns dual-pane canonical-event fan-out into
  presentation-only state; it does not execute, approve, or persist.
- `frame_composition.py` owns stateless bounded dual-pane line/scene
  composition from canonical projection state; it does not reduce events or
  authorize operator actions.
- `chassis_composition.py` owns stateless dual-pane aperture and control-rail
  geometry around already-composed pane content; it does not own projection,
  lifecycle, or task state.
- `glass_presentation.py` owns optional Kitty pixel-layer placement and
  retained Glass image identities; it does not own terminal lifecycle,
  projection state, or task semantics.
- `dual_pane_lifecycle.py` owns terminal open/close and Glass animation
  invalidation through callbacks; it does not own projection or task state.
