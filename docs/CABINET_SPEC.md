# AthenaBOX cabinet contract

This is the visual contract for the native AthenaBOX compositor. The attached
AthenaBOX(6) reference is authoritative for the physical enclosure; DAGOAL is
authoritative for the live scene inside the right CRT.

## Reference and resize behavior

- Reference design space: **1672 × 941**.
- The complete cabinet is uniformly scaled by `min(window_width / 1672,
  window_height / 941)` and centered in the window.
- Unused space is letterboxed. The cabinet is never independently stretched
  horizontally or vertically.
- The native window advertises a usable minimum size of 900 × 620. The
  internal layout remains safe and compact if a window manager supplies a
  smaller surface.
- The borderless surface keeps a 12-pixel interactive perimeter: eight
  edge/corner zones provide directional resize cursors and EWMH moveresize
  requests, while the header fascia remains the move handle.
- The five acceptance surfaces are 1672 × 941, 1920 × 1080, 1280 × 800,
  1280 × 720, and 1170 × 659. A live process must survive the sequence
  1280 × 800 → 1672 × 941 → 1170 × 659 → 1920 × 1080 → 1280 × 720.

## Authoritative physical hierarchy

The compositor renders these layers in order:

1. neutral graphite outer shell and broad value gradients;
2. raised fascia, seams, and material texture;
3. distinct recessed operator and Glass Compute wells;
4. lower instrument deck and physical controls;
5. screen content;
6. physical-resolution glass edge falloff, reflection, and convex highlight.

The left well is a comparatively flat rectangular operator display. The right
well is a deeper, rounder CRT assembly with a dark inner rim and restrained
blue-gray glass. Hardware is neutral graphite; blue/green/amber light belongs
to screens, lamps, and engraved labels.
The display assembly is dominant and the lower control rail is intentionally
shallow; the identity/data plate is an independent far-right module after the
power control.

## Reference-space feature groups

The normalized geometry is owned by `NativePixelLayout` and `RailLayout`:

- top fascia and `GLASS COMPUTE ENGINE` lamp;
- equal-sized operator and OI apertures;
- operator transcript viewport inside its bezel;
- 384 × 256 logical DAGOAL scene inside the right CRT;
- lower speaker, operator prompt module, system lamps, primary encoder,
  brightness, focus, power, and identity plate.

The prompt module is a rectangular recessed equipment bay. Its status, input,
and hint rows are measured from the live Xft role metrics. At small sizes the
hint is omitted first, then status; the input row is clamped as a last resort.
No row may draw outside the prompt rectangle.
Physical module labels use a clipped bitmap design layer and are placed in
reference units. Dynamic Xft text is limited to live transcript/prompt/status
content.

## Dynamic ownership

- The left display owns conversation/transcript history and uses the PTY size
  derived from its transcript viewport.
- The right CRT owns current task state. Its low-resolution scene uses semantic
  workspace/runtime objects, actual code previews, verification checks,
  Buddy pose/frame state, and true perspective paths.
- Approvals and noteworthy notifications are derived from projection
  `attention_items`. They appear as a bounded right-edge rail inside the CRT;
  they never replace the live scene. Execution gating remains owned by the
  service/policy layer.
- Scanlines and pixel graphics are rendered in the 384 × 256 target. Glass
  treatment is applied after that target is composed into the physical well.

## Evidence required for visual changes

Use a fixed 1672 × 941 capture and compare the silhouette, fascia, wells,
divider, lower deck, bezel depth, glass, controls, then materials in that
order. A native runtime can emit:

```bash
ATHENA_NATIVE_LAYOUT_DUMP=/tmp/athena-layout.json \
ATHENA_NATIVE_OI_DUMP=/tmp/athena-oi.png \
athena native
```

The layout dump includes `metrics_source: "live_xft"` when produced by a live
X11 renderer; `fallback_static` is explicitly marked when no X display is
available. The OI dump is always 384 × 256 when the OpenGL target is active.
The deterministic physical-shell evidence command is:

```bash
scripts/native-cabinet-golden
```

It compares `native/assets/athenabox/cabinet-golden.png` at 1672 × 941; the
baseline is mandatory whenever Xvfb/ImageMagick are available.
