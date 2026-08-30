# Native Athena terminal frontend

Athena is being developed with three frontends over one UI model:

```text
shared projection / layout / scene / input contracts
                         │
          ┌──────────────┼──────────────┐
          │              │              │
      native          hosted Glass    ANSI/plain
   Athena window   Kitty/WezTerm/Pillow compatibility
```

The native frontend is the canonical visual destination. It uses an
upstream-tracking Alacritty terminal core and adds an Athena compositor for
the graphite chassis, operator well, convex CRT, instrument rail, and DAGOAL
scene. Athena-specific compositor code remains separate from the terminal
engine so terminal updates do not become UI rewrites.

The current Python CLI implements the shared model, the ANSI instrument, and
the hosted Glass renderer. Glass speaks the Kitty Graphics Protocol, which is
implemented by Kitty and WezTerm; Athena does not need a separate WezTerm
renderer. The Rust crate in `native/` now includes the first native executable:
it owns a PTY, feeds the pinned Alacritty terminal core, renders
`RenderableContent` cells through Fontconfig/Xft, composes the chassis and OI
scene in OpenGL, handles focus/keyboard/resize events, and accepts semantic
projection frames containing model-request identity, workspace/runtime trees,
structured diagnostics, and alerts.
`athena.cli.native_bridge` emits those newline-delimited frames from the same
`ProjectionState` used by hosted Glass and `oi-stream`. The Python bridge does
not choose pixel placement or stream animation frames; Rust animates the OI
locally from semantic state. Linux/X11 provides UTF-8/IME lookup when the host
input method is available, mouse selection, focus indication, navigation keys,
and Ctrl-Shift-C/Ctrl-Shift-V clipboard integration over the Alacritty grid.
Escape is forwarded to the PTY; only the window manager close request closes
the native surface.

During development, `athena native` launches the native executable with a
Python Athena service session as its PTY child. The child publishes the same
projection state over `ATHENA_NATIVE_BRIDGE_SOCKET`; the bridge is local to the
native process and carries no credentials. Build the binary with:

```bash
cargo build --manifest-path native/Cargo.toml --offline
athena native --mascot owl
```

Presentation controls are optional and never affect agent authority:

```bash
athena native --mascot cat --no-animations
athena native --reduced-motion
```

Use `athena native` for the AthenaBOX window. `athena chat` remains the
host-terminal conversation surface and does not open this native compositor.

The native frontend must preserve these boundaries:

- terminal/parser, PTY, selection, clipboard, and input remain terminal-engine
  responsibilities;
- Athena projection state remains read-only and event-driven;
- the OI scene is clipped to the fixed right CRT aperture;
- terminal text, cursor, selection, and PTY resize all use the same operator
  content rectangle;
- animation changes presentation, never task authority or semantic progress;
- ANSI and plain/headless paths remain first-class for SSH and CI.
