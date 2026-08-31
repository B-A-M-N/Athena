# Athena terminal frontend (development preview)

This directory contains the first native frontend boundary for the Athena
terminal application. It is not a second agent runtime and it is not a copy of
the Python CLI.

The frontend tracks the upstream Alacritty terminal engine for PTY and parsing
semantics. An
Athena-owned compositor adds the instrument chassis, equal operator/OI
apertures, CRT treatment, central divider, control deck, and OpenGL OI scene.
Structured projections render workspace/runtime text columns while the
perspective graph remains the fallback for sparse frames. The compositor
consumes the shared projection contracts from `src/athena/cli/` through a
semantic bridge rather than reimplementing task semantics.

The first native vertical slice now exists as `athena-terminal`. It owns a PTY,
feeds output through the pinned Alacritty terminal core, renders its
`RenderableContent` cells with Fontconfig/Xft, accepts UTF-8/IME keyboard input,
focus and resize events in its Linux/X11 window path, and consumes semantic
JSON projection frames without taking authority over Athena state. Projection
frames carry structured scene entities, model-request identity,
workspace/runtime trees, diagnostics, and alerts as well as terminal text; the
OpenGL compositor renders them as an AthenaBOX chassis, deep-bezel displays,
workspace/runtime OI view, and a quantized Buddy. Mouse selection,
Ctrl-Shift-C/Ctrl-Shift-V clipboard round-trips, scrollback, navigation keys,
and explicit prompt states are supported. The lower prompt is a Rust-owned
Unicode line editor and the PTY receives only completed lines. Ctrl-C cancels
the foreground task without closing the session; `/exit` closes it. Projection
schema 3 carries current action/query/progress context, while schema 2 is
normalized and unknown schemas are rendered as bridge errors. Font-derived
metrics are the source of truth for layout, text, hit testing, and PTY size;
motion-only OI updates use a retained dirty region.

For a headless PTY/core/bridge smoke check:

```bash
cargo run --manifest-path native/Cargo.toml --offline -- \
  --headless --bridge-stdin --command 'printf "native-ok\\n"'
```

For the native window:

```bash
cargo build --manifest-path native/Cargo.toml --offline
athena native --mascot owl
```

The native bridge is a local Unix socket and carries no credentials. Run the
headless, input, and exact-window visual checks from the repository root with
`scripts/native-smoke`, `scripts/native-input-smoke`, and
`scripts/native-visual-smoke`.

`athena chat` is not the native window; it selects a host-terminal surface.
Use `--no-animations` or `--reduced-motion` for deterministic/static OI
presentation.

For the current hosted paths, use:

```text
athena --display glass   # hosted raster OI, confirmed Kitty transport
athena --display ansi    # universal instrument fallback
athena --display plain   # line-oriented fallback
```

The native work remains separate from hosted Glass. Kitty and WezTerm are
compatible hosts for the Python renderer, while this frontend owns its window
and compositor instead of asking another terminal to interpret graphics
escapes. `athena native` launches a Python service session as the PTY child and
connects its canonical event projection through a Unix socket; set
`ATHENA_NATIVE_BIN` when the binary lives outside the development tree.
