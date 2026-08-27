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

The native frontend is the canonical visual destination. It will use an
upstream-tracking Alacritty terminal core/rendering foundation and add an
Athena compositor for the chassis, operator well, CRT, instrument rail, and
OI scene. Athena-specific compositor code must remain separate from the
upstream terminal engine so terminal updates do not become UI rewrites.

The current Python CLI implements the shared model, the ANSI instrument, and
the hosted Glass renderer. Glass speaks the Kitty Graphics Protocol, which is
implemented by Kitty and WezTerm; Athena does not need a separate WezTerm
renderer. The Rust crate in `native/` now includes the first native executable:
it owns a PTY, feeds the pinned Alacritty terminal core, has a Linux/X11
OpenGL proof compositor, handles keyboard/resize events, and accepts serialized
projection frames containing terminal text plus structured scene entities and
alerts. `athena.cli.native_bridge` emits those newline-delimited frames from
the same `ProjectionState` used by hosted Glass and `oi-stream`. The glyph
renderer, cross-platform window backends, and live Python service bridge remain
in development. The Linux/X11 proof path now provides mouse selection and
Ctrl-Shift-C/Ctrl-Shift-V clipboard integration over the Alacritty grid.

During development, `athena native` launches the native executable with a
Python Athena service session as its PTY child. The child publishes the same
projection state over `ATHENA_NATIVE_BRIDGE_SOCKET`; the bridge is local to the
native process and carries no credentials. Build the binary with:

```bash
cargo build --manifest-path native/Cargo.toml --offline
athena native
```

The native frontend must preserve these boundaries:

- terminal/parser, PTY, selection, clipboard, and input remain terminal-engine
  responsibilities;
- Athena projection state remains read-only and event-driven;
- the OI framebuffer is clipped to the fixed right CRT aperture;
- animation changes presentation, never task authority or semantic progress;
- ANSI and plain/headless paths remain first-class for SSH and CI.
