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
the graphite chassis, deep-bezel operator/OI displays, central divider,
control deck, and DAGOAL scene. Athena-specific compositor code remains
separate from the terminal engine so terminal updates do not become UI
rewrites.

The current Python CLI implements the shared model, the ANSI instrument, and
the hosted Glass renderer. Glass speaks the Kitty Graphics Protocol, which is
implemented by Kitty and WezTerm; Athena does not need a separate WezTerm
renderer. The Rust crate in `native/` now includes the first native executable:
it owns a PTY, feeds the pinned Alacritty terminal core, renders
`RenderableContent` cells through Fontconfig/Xft, composes the chassis and OI
scene in OpenGL, handles focus/keyboard/resize events, and accepts semantic
projection frames containing model-request identity, workspace/runtime trees,
structured diagnostics, and alerts.
Structured OI frames render the reference console's workspace-map/runtime-tree
information and Buddy display; the perspective graph is reserved for
projections that do not provide structured tree data.
`athena.cli.native_bridge` emits those newline-delimited frames from the same
`ProjectionState` used by hosted Glass and `oi-stream`. The Python bridge does
not choose pixel placement or stream animation frames; Rust animates the OI
locally from semantic state. Projection frames use schema 3; schema 2 frames
are normalized for compatibility and unknown schemas become a visible bridge
error. The bridge includes the current action kind, label, target/query,
detail, progress, model identity, runtime tree, and recent stream tail, so the
native OI does not have to guess what the agent is doing.

Linux/X11 provides UTF-8/IME lookup with overflow-safe buffers, mouse
selection, focus indication, navigation keys, scrollback, and
Ctrl-Shift-C/Ctrl-Shift-V clipboard integration over the Alacritty grid. The
lower prompt is a Rust-owned Unicode line editor: arrows, Home/End, history,
selection, word erase, insertion, and bounded caret rendering are native;
Python receives only completed lines at the service boundary. Ctrl-C clears
the prompt and cancels the foreground service task while keeping the session
alive; `/exit` is the explicit session close command. Alternate-screen
programs receive application-cursor key sequences, and bracketed paste is
sanitized and wrapped according to the terminal mode. Escape is forwarded to
the PTY; only a `WM_PROTOCOLS`/`WM_DELETE_WINDOW` client message closes the
native surface.

The X11 layout has one physical-pixel authority. Fontconfig/Xft metrics drive
the operator cell size, baseline, PTY resize, text placement, and prompt hit
testing. The CRT scene is scissored to its aperture and long labels/previews are
clipped or truncated. Static chassis pixels remain retained in the
single-buffer drawable; terminal output repaints only the operator aperture,
and motion-only frames redraw only the OI aperture. Projection changes,
resize, focus, selection, and prompt edits invalidate the full frame. The
lower control deck contains the Rust prompt, scroll/edit guidance, speaker
grille, activity meter, and labeled presentation controls; bridge state remains
the only connectivity label.

During development, `athena native` launches the native executable with a
Python Athena service session as its PTY child. The child publishes the same
projection state over `ATHENA_NATIVE_BRIDGE_SOCKET`; the bridge is local to the
native process and carries no credentials. Build the binary with:

```bash
cargo build --manifest-path native/Cargo.toml --offline
athena native --mascot owl
```

The native binary must be built first. `athena native` starts the Python
service session inside the PTY and connects its local Unix-socket projection
bridge; credentials are read by the service configuration and are never
serialized into the projection bridge.

Presentation controls are optional and never affect agent authority:

```bash
athena native --mascot cat --no-animations
athena native --reduced-motion
```

Use `athena native` for the AthenaBOX window. `athena chat` remains the
host-terminal conversation surface and does not open this native compositor.

For deterministic native checks:

```bash
scripts/native-smoke
scripts/native-input-smoke
scripts/native-visual-smoke
```

The input smoke asserts the exact line received by a PTY child, including
middle editing and Unicode, then checks Ctrl-C cancellation leaves the window
usable. The visual smoke captures the exact Athena window (not the X11 root)
and checks dimensions, color complexity, and contrast. Both X11 checks skip
with an explicit reason when a usable Xvfb display is unavailable.

The native frontend must preserve these boundaries:

- terminal/parser, PTY, selection, clipboard, and input remain terminal-engine
  responsibilities;
- Athena projection state remains read-only and event-driven;
- the OI scene is clipped to the fixed right CRT aperture;
- terminal text, cursor, selection, and PTY resize all use the same operator
  content rectangle;
- projection debounce coalesces bursts without a lost-update handoff race;
- animation changes presentation, never task authority or semantic progress;
- ANSI and plain/headless paths remain first-class for SSH and CI.
