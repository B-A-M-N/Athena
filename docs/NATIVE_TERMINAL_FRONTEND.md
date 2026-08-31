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
Structured OI frames drive a low-resolution DAGOAL scene: workspace/runtime
objects, actual code previews, verification gates, model activity, Buddy
poses, and localized diagnostics all come from the shared projection. The
right CRT is a current-state observability surface, not a second transcript;
the left operator display retains conversation history.
`athena.cli.native_bridge` emits those newline-delimited frames from the same
`ProjectionState` used by hosted Glass and `oi-stream`. The Python bridge does
not choose pixel placement or stream animation frames; Rust animates the OI
locally from semantic state. Projection frames use schema 3; schema 2 frames
are normalized for compatibility and unknown schemas become a visible bridge
error. The bridge includes the current action kind, label, target/query,
detail, progress, model identity, runtime tree, recent stream tail, and bounded
`attention_items`, so the native OI does not have to guess what the agent is
doing. Approvals and noteworthy notifications appear in a non-modal right-edge
rail inside the CRT.

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

The X11 layout has one physical-pixel authority in a canonical 1672×941
AthenaBOX design space. The complete cabinet is uniformly scaled and
letterboxed at other window ratios. Fontconfig/Xft metrics drive the operator
cell size, baseline, PTY resize, text placement, and prompt hit testing.
Prompt rows are derived from separate body/input/heading/instrument metrics;
the status, editable input, hint, and caret are independently clipped and
cannot escape the prompt bay. The CRT scene is rendered into a retained
384×256 logical framebuffer, sampled with
nearest-neighbor filtering into the physical aperture, and clipped through an
8-bit rounded stencil when the selected X11 visual supports one (with a
rectangular compatibility fallback). A physical-resolution glass pass adds
restrained edge falloff, reflection, and convex highlights after the logical
scene is composed; scanlines remain in the logical target. Renderer modules own
primitives, chassis, prompt, terminal cells, OI scene, Buddy sprites, font
roles, and frame composition; `x11.rs` owns platform events and lifecycle.
Terminal output repaints only the operator aperture, and motion-only frames
redraw only the OI aperture.
The ownership split is explicit: `render/frame.rs` is the composition
boundary, `render/chassis.rs` owns physical shell/rail detail and the bundled
`native/assets/athenabox/chassis_noise.ppm` material,
`render/oi.rs` owns semantic scene grammar and the OI target,
`render/terminal.rs` owns Alacritty cell presentation, `render/prompt.rs`
owns the instrument prompt, `render/text.rs` owns Xft font metrics/caches,
and `render/primitives.rs` owns OpenGL geometry and clipping. The platform
boundary keeps XIM lookup in `native/src/platform/input_method.rs` and
selection ownership in `native/src/platform/clipboard.rs`. Buddy data and
pose vocabulary are isolated under `native/src/buddy/`, with separate Owl,
Cat, and Bot sprite modules.
Projection changes, resize, focus, selection, and prompt edits invalidate the
full frame. The lower control deck follows the reference rail: speaker,
operator prompt/status, system lamps, primary encoder, brightness, focus,
power, and identity plate. Brightness and focus change OI presentation, while
power toggles the OI display without affecting the agent session. The window
uses a borderless Motif hint while preserving header-fascia drag through the
WM moveresize protocol.

During development, `athena native` launches the native executable with a
Python Athena service session as its PTY child. The child publishes the same
projection state over `ATHENA_NATIVE_BRIDGE_SOCKET`; the bridge is local to the
native process and carries no credentials. Build the binary with:

```bash
cargo build --manifest-path native/Cargo.toml --offline
athena native
```

The native binary must be built first. `athena native` starts the Python
service session inside the PTY and connects its local Unix-socket projection
bridge; credentials are read by the service configuration and are never
serialized into the projection bridge.

Owl is the built-in default. Temporary Buddy overrides are optional:

```bash
athena native --mascot cat
athena native --mascot bot
athena native --mascot off
athena native --no-animations
athena native --reduced-motion
```

Use `athena native` for the AthenaBOX window. `athena chat` remains the
host-terminal conversation surface and does not open this native compositor.

For deterministic native checks:

```bash
scripts/native-smoke
scripts/native-input-smoke
scripts/native-visual-smoke
scripts/bench-rendering --require-native
```

The input smoke asserts the exact line received by a PTY child, including
middle editing and Unicode, then checks Ctrl-C cancellation leaves the window
usable. The visual smoke captures the exact Athena window (not the X11 root)
for 27 fixtures: idle, short typing, 80+ character typing with cursor movement,
search, read, code, test, approval, and failure, each with Owl, Cat, and Bot.
It checks dimensions, color complexity, contrast, the current rail schema, and
a real 384×256 OI framebuffer dump. `--dump-layout` uses live Xft metrics when
an X display is available and explicitly marks `fallback_static` otherwise;
`ATHENA_NATIVE_LAYOUT_DUMP` records the live runtime dump. The rendering benchmark
separates idle, terminal activity, and semantic OI animation probes; the OI
probe is fed a real search projection rather than merely printing terminal
lines. Set `NATIVE_VISUAL_GOLDEN_DIR=/path/to/goldens` to compare captured
fixtures against reviewed ImageMagick RMSE goldens; set
`NATIVE_VISUAL_UPDATE_GOLDENS=1` only when intentionally refreshing that
directory. No goldens are treated as approved until a human reviews the
captures. All X11 checks skip with an explicit reason when a usable Xvfb
display is unavailable.

The native frontend must preserve these boundaries:

- terminal/parser, PTY, selection, clipboard, and input remain terminal-engine
  responsibilities;
- Athena projection state remains read-only and event-driven;
- the OI scene is clipped to the fixed right CRT aperture;
- the right CRT scene is semantic and current-state oriented; durable prose
  remains in the left transcript;
- approvals are execution gates owned by policy, but presentation overlays
  remain non-modal and never hide the live scene;
- terminal text, cursor, selection, and PTY resize all use the same operator
  content rectangle;
- projection debounce coalesces bursts without a lost-update handoff race;
- animation changes presentation, never task authority or semantic progress;
- ANSI and plain/headless paths remain first-class for SSH and CI.
