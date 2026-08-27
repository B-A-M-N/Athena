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
alerts. The glyph renderer, selection/clipboard integration,
cross-platform window backends, and live Python service bridge remain in
development.

The native frontend must preserve these boundaries:

- terminal/parser, PTY, selection, clipboard, and input remain terminal-engine
  responsibilities;
- Athena projection state remains read-only and event-driven;
- the OI framebuffer is clipped to the fixed right CRT aperture;
- animation changes presentation, never task authority or semantic progress;
- ANSI and plain/headless paths remain first-class for SSH and CI.
