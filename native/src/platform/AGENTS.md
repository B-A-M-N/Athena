# Native Platform

## Purpose

Platform modules own X11/window/input/selection mechanics for the native
frontend.

## Ownership

Platform code owns OS integration only. Presentation semantics remain in
render modules and application truth remains in the projection producer.

## Local Contracts

- FFI, window lifecycle, event translation, clipboard, and sizing stay here.
- Platform code must not invoke Python application logic or decide task/model/capability state.
- Extract stable mechanical modules before expanding the X11 host.

## Work Guidance

Keep unsafe FFI isolated and make event translation explicit and testable.

## Verification

Run native check/test commands and the desktop smoke lane when available.

## Child DOX Index

| Path | Contract |
|------|----------|
| `mod.rs` | Platform module boundary and narrow re-exports to the X11 host. |
| `ffi.rs` | Crate-visible X11/Xft/GL ABI declarations and constants only. |
| `clipboard.rs` | X11 selection ownership and clipboard event translation. |
| `input_method.rs` | XIM/keyboard translation and bounded lookup behavior. |
| `surface.rs` | Offscreen X11/GLX presentation surface lifecycle and region copy. |
| `telemetry.rs` | Native presentation timing and render telemetry sinks. |
| `terminal.rs` | Terminal-grid resize mechanics and PTY sizing. |
