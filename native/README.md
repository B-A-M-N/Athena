# Athena terminal frontend (development preview)

This directory contains the first native frontend boundary for the Athena
terminal application. It is not a second agent runtime and it is not a copy of
the Python CLI.

The frontend tracks the upstream Alacritty terminal engine for PTY and parsing
semantics. An
Athena-owned compositor will add the instrument chassis, equal operator/OI
apertures, CRT treatment, lower rail, and the raster OI scene. The compositor
will consume the shared projection contracts from `src/athena/cli/` through a
small bridge rather than reimplementing task semantics.

The first native vertical slice now exists as `athena-terminal`. It owns a PTY,
feeds output through the pinned Alacritty terminal core, accepts keyboard input
and resize events in its Linux/X11 window path, and consumes JSON projection
frames without taking authority over Athena state. Projection frames can carry
structured scene entities and alerts as well as terminal text; the OpenGL
compositor renders those as a sparse CRT graph with one Buddy. The Linux/X11
proof path also supports mouse selection plus Ctrl-Shift-C/Ctrl-Shift-V
clipboard round-trips. It remains a small proof surface, not the finished
glyph/shader renderer; cross-platform window backends and the Python service
bridge are still in development.

For a headless PTY/core/bridge smoke check:

```bash
cargo run --manifest-path native/Cargo.toml --offline -- \
  --headless --bridge-stdin --command 'printf "native-ok\\n"'
```

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
