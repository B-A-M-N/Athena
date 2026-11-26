# Native Source

## Purpose

This directory contains the native executable and its presentation/platform
modules.

## Ownership

The executable coordinates the PTY and projection lifecycle. Child modules own
their narrow rendering or OS concerns.

## Local Contracts

- Keep the bridge DTO stable and versioned.
- Do not import or reproduce Python business rules in Rust.
- Size ratchets are decomposition triggers, not documentation targets.

## Work Guidance

Use small modules with explicit inputs and outputs. Avoid making `main.rs`,
`x11.rs`, or `render/oi.rs` the destination for unrelated UI behavior.

## Verification

Run the native check and test commands from the parent contract after Rust
changes, including the locked offline build used by CI.

## Child DOX Index

| Path | Purpose |
|------|---------|
| `platform/` | OS/window/input mechanics |
| `render/` | Projection-driven presentation |
| `x11.rs` | X11 event-loop and frame orchestration |
| `x11/event_loop.rs` | Persistent pointer-drag session state and WM fallback timing |
| `x11/frame_runtime.rs` | Frame cadence, dirty-domain presentation, capture sync, and render counters |
| `x11/layout_diagnostics.rs` | Live Xft layout snapshots and bounded runtime layout dumps |
| `x11/window_runtime.rs` | X11 window setup, event dispatch, and lifecycle teardown |
| `x11/window_input.rs` | XIM keyboard lookup, prompt editing, terminal bytes, zoom, and presentation controls |
| `x11/window_pointer.rs` | Pointer scrolling, selection, presentation controls, and WM drag/resize gestures |
| `x11/window_resize.rs` | Configure-notify surface rebinding, font/PTY resize, and resize diagnostics |
| `x11/window_setup.rs` | X11 window, GLX context, and presentation-surface acquisition/teardown |
| `x11/window_manager.rs` | WM strategy/telemetry, pointer grabs, resize cursors, and EWMH moveresize protocol |
| `projection_schema.rs` | Projection bridge DTO schema, version constants, and animation state |
| `render/chassis_panels.rs` | Recessed panel/control drawing primitives used by the chassis renderer |
| `render/fit.rs` | Pure text-fitting/color conversion helpers shared by render surfaces |
| `x11/window_hints.rs` | WM property writes: size hints, Motif decorations, `_NET_WM_PID` |
| `window_management.rs` | EWMH/fallback window movement and resize policy |
