# SHELL_HARDENING.md
## Athena Visual Shell — Audit & Implementation Notes

> **Document status (2026-08-29): historical visual audit.** This file records
> the reference-image gaps and proposed implementation notes captured during
> an earlier shell review. It is not the current release checklist. Current
> implementation truth is in `README.md` and `docs/ARCHITECTURE.md`: the ANSI
> surface is the portable fallback, the hosted Glass surface owns the active
> animation layer, and the native frontend remains a development preview.

> **Reference images**
> - **DA GOAL** — defines the desired animation style and visual detail level (scan-line CRT Glass window, dot-matrix Buddy figure, perspective grid, multi-panel OI layout with live viewport marker, progress bars, diagnostic cards, status lamps, knob-row, bottom status rail).
> - **Athenabox** — defines the correct physical *shape* of the shell (a single instrument chassis rendered as a physical control surface / vintage CRT cabinet; two inset rounded-rectangle apertures sitting inside a darker outer bezel; physical speaker grille, knob row, status LED rail all below the screens; top header bar with logo, subtitle, and toggle chip).

---

## Part 1 — Audit: What Is Already Implemented

### ✅ Implemented Correctly

| Capability | Where |
|---|---|
| Dual-aperture equal-width layout (operator left / OI right) | `layout.py` – `compute_layout()` |
| GLASS_FULL / GLASS_COMPACT / ANSI_INSTRUMENT / PLAIN responsive breakpoints | `layout.py:82-88` |
| CRT Glass viewport – Pillow PNG rasterizer | `framebuffer.py` – `OIFrameBuffer` |
| Deep blue-black CRT palette | `framebuffer.py:186-191` |
| Perspective vanishing-point grid lines | `framebuffer.py:177-180` |
| Horizontal phosphor scan-line texture | `framebuffer.py:181-182` |
| Corner glass-highlight arc | `framebuffer.py:318` |
| Workspace MAP + RUNTIME GRAPH split panel (OI default scene) | `framebuffer.py:237-319` |
| Rounded operation node boxes with status colour | `framebuffer.py:283-290` |
| ACTION screens: CODE diff, TEST bar, VERIFY checklist, FAILURE diagnostics, APPROVAL, RECOVER, SEARCH | `framebuffer.py:322-515` |
| LIVE TRACE stream band at bottom of OI | `framebuffer.py:294-313` |
| Multi-phase animation clock (10 FPS active / 2 FPS settled) | `animation.py:104-148` |
| Phase clocks: scan, pulse, cursor, grid, activity, code-reveal | `animation.py:89-100` |
| Three-layer Kitty compositing (base / motion / buddy overlay) | `dual_pane.py:1425-1482` |
| Smoothstep eased Buddy anchor transitions | `framebuffer.py:136-144` |
| Buddy procedural pixel figure (body, screen, eyes, antenna, feet, beak/ears) | `framebuffer.py:754-1035` |
| Eye scan / directional shift during READING, SEARCHING | `framebuffer.py:804-807` |
| Mode-specific Buddy poses (CODE arms, TEST/VERIFY magnifier, APPROVAL card, FAILURE X) | `framebuffer.py:895-1027` |
| Scanning beam + particle trail motion overlay | `framebuffer.py:607-637` |
| Pulsing failure / approval border in motion overlay | `framebuffer.py:638-660` |
| ANSI diff renderer (cell-accurate, no full-repaint flicker) | `render/ansi.py` |
| ANSI scene renderer with collision-safe Buddy placement | `render/scene.py` |
| ASCII mascot (owl / cat / bot) with frame animation | `dual_pane.py:160-700` |
| Model-request label in header | `dual_pane.py:1348-1351` |
| Measured status rail with task/surface/OI/model state; unavailable hardware controls stay neutral | `dual_pane.py:_frame_lines()` |
| Reduced-motion / `--no-animations` flags | `animation.py:51` |
| Event-truthful state machine (no synthetic progress) | throughout |
| OI scene animations extended to: APPROVAL, RECOVER, GENERATE, SEARCH, TEST, VERIFY, CODE, FAILURE | `framebuffer.py:605-688` |

---

## Part 2 — Historical Gaps: What Was Missing vs. the Reference Images

## Current implementation matrix (2026-08-29)

The sections below intentionally retain the original visual critique. The
following matrix is the authoritative status for this beta and points to the
current code paths:

| Surface | Current truth | Evidence |
|---|---|---|
| ANSI fallback | Static, event-truthful operator/OI projection with measured rail; no scan animation | `src/athena/cli/dual_pane.py`, `tests/unit/cli/test_dual_pane.py` |
| Hosted Glass | Pillow/Kitty compositor with scan, pulse, grid, and Buddy animation | `src/athena/cli/framebuffer.py`, `src/athena/cli/animation.py` |
| Native frontend | Alacritty PTY/core plus serialized recursive workspace/runtime projection; development preview | `native/src/main.rs`, `native/src/x11.rs`, `scripts/native-smoke` |
| Hardware telemetry | Not implemented; BRIGHTNESS/FOCUS render unavailable and VIEW reports frontend state | `src/athena/cli/dual_pane.py:_frame_lines()` |

Reference-image fidelity items not listed as implemented remain deliberately
deferred visual work; they are not release claims.

### GAP-01 — Shell Outer Bezel / Physical Cabinet Shape — DEFERRED

**What the images show (Athenabox):**
- A dark, nearly-black outer chassis surround — a physical *bezel* extending several rows/columns beyond the inner apertures. It looks like a desk instrument / CRT monitor body.
- The outer bezel is textured differently from the aperture interiors (slightly lighter, with subtle horizontal ribbing or matte relief).
- The two monitor "screens" sit *inset* into this bezel with a visible raised rim / shadow around each aperture — rounded-rect inside a larger rounded-rect, not just a box-drawing border.
- Top of the chassis has a dedicated *header bar* with: `ATHENA  OPERATOR INSTRUMENT  //` left-aligned and a `GLASS COMPUTE ENGINE [ ]` chip toggle right-aligned.

**Current state:**
- `dual_pane.py:1379-1392` draws `╭──────────────────────────╮` / `╰──────────────────────────╯` — a *single flat box* around both panes. There is no outer chassis bezel, no inset aperture effect, no rim shadow.
- The left/right inner edges use `▐▌` block characters as pseudo-bevels but these are inside the single box, not a nested structure.

---

### GAP-02 — Inset Aperture Rim / CRT Bezel Inset — DEFERRED

**What the images show:**
- Each monitor screen (left and right) has its own distinct rounded-rectangle bezel — a second layer of nesting. The screen content sits *inside* a slightly recessed aperture with rounded corners different from the outer chassis corners.
- In the Athenabox image especially, the left screen has a notably deep corner radius on its own bezel, making it visually "sit inside" the chassis rather than being a flat pane division.

**Current state:**
- There is no second-level aperture border. The inner panels are delineated only by the `▐`/`▌` bevel fill and `░░░` seam characters. No second rounded-rectangle ring exists around each screen.

---

### GAP-03 — Bottom Hardware Row: Speaker Grille, Knob Row, LED Rail — DEFERRED

**What the images show (both images):**
- Bottom strip below the two screens contains (left to right):
  - A **speaker grille** — dot-matrix or vertical bar pattern in a small rectangular cell
  - A **status text area** ("Athena is working through the request." + `> █` prompt cursor)
  - **STATUS indicator section** with labelled LEDs: `SYSTEM ●  NETWORK ●  ACTIVITY ●` (Athenabox) / `SYS ●  NET ●  IO ●  MODEL ●` (DA GOAL)
  - A **large rotary knob** (rendered as a circle with a notch)
  - Separate **BRIGHTNESS**, **FOCUS**, **POWER** knob cluster (right half)
  - A **model label panel** (DA GOAL): `ATHENA / OI / GLASS COMPUTE / MODEL OI-1 / SERIAL 0001-A`
  - A **RUNNING indicator** LED (dot, coloured)

**Current state:**
- `dual_pane.py:1395-1406` renders a control rail as two text lines:
  - `─────────────────────────────`
  - `SYS ●   NET ●   IO ●   MODEL <model_label>   TASK <status>`
  - `─────────────────────────────`
  - `❯ <prompt_text>  Ctrl-C cancel · Ctrl-D exit`
- **Missing:** speaker grille glyph, rotary knob glyphs, BRIGHTNESS/FOCUS/POWER knob cluster, model/serial label panel, RUNNING LED, and the visual division of the bottom row into distinct instrument sections.

---

### GAP-04 — Top Header: "LIVE VIEWPORT" Label + Right-Side Status Chip — DEFERRED

**What the images show (DA GOAL):**
- Right pane title row shows `ATHENA OI // GLASS COMPUTE` left and `LIVE VIEWPORT` right-aligned — a persistent label in the header of the OI aperture.
- Both images show a top-right chip/toggle: `GLASS COMPUTE ENGINE [ ]` in Athenabox; `LIVE VIEWPORT` inline in DA GOAL.

**Current state:**
- `dual_pane.py:1363` sets `right_title = "OI // HISTORY" if self._right_scroll else "ATHENA OI // GLASS COMPUTE"`.
- No `LIVE VIEWPORT` suffix label is appended. No right-aligned chip/toggle exists in the header row.

---

### GAP-05 — Dot-Matrix / Dithered Buddy Pixel Style — DEFERRED

**What the images show (DA GOAL, right pane):**
- The Buddy/mascot figure in the OI panel is rendered in a **dithered dot-matrix** or **stippled pixel grid** style — the body is composed of discrete square pixel blocks (`▪`, `█`, `░`, `▒`) arranged to give a retro low-resolution monitor character look, matching the CRT aesthetic.
- It looks like a 16×16 or 32×32 dithered bitmap sprite, NOT a smooth vector illustration.
- The character has visible pixel boundary aliasing consistent with a CRT phosphor screen rendering.

**Current state:**
- `framebuffer.py:754-1035` `_draw_buddy()` uses Pillow's `draw.rounded_rectangle()`, `draw.ellipse()`, `draw.line()`, `draw.arc()`, `draw.polygon()` — smooth vector/anti-aliased primitives. There is no pixel quantization, dithering, or discrete pixel-block rasterization.
- The ANSI mascot in `dual_pane.py:160-242` uses box-drawing ASCII art but not a dot-matrix pixel approach.

---

### GAP-06 — OI Panel: MODEL REQUEST Header Row — DEFERRED

**What DA GOAL shows:**
- At the very top of the OI pane content (below the title bar), a dedicated row shows: `> MODEL REQUEST · fake/fake-1`
- Below it: `> ACTIVE OPERATION`
- These are styled as expandable tree items with `>` chevrons.

**Current state:**
- `framebuffer.py:195-202` renders `scene.title` and a `MODE / VIEWPORT` status line, but does not include a dedicated `MODEL REQUEST · <model>` row with the `>` chevron prefix.
- `render/scene.py:349-351` starts the ANSI canvas with `WORKSPACE MAP` / `RUNTIME GRAPH` with no model request row.

---

### GAP-07 — Workspace File Tree: Inline Status Glyphs with `[✓]` / `[...]` Badges — DEFERRED

**What DA GOAL shows:**
- The workspace file listing uses tree-drawing characters (`├─`, `└─`) with right-aligned status badges: `[✓] read`, `[...] testing`.
- Files are shown as tree indented branches, not flat list items.

**Current state:**
- `framebuffer.py:243-256` renders entities as a flat `· label` or `✓ label` or `! label` list. No tree-draw characters (`├─`, `└─`, `│`). No right-aligned badge tokens.
- `render/scene.py:353-360` similarly flat.

---

### GAP-08 — Runtime Tree: Hierarchical Box-Node Graph with Edge Lines — PARTIAL

**What the images show:**
- DA GOAL right side shows a `RUNTIME TREE` with box nodes (`verify-release / task`, `inspector / module`, etc.) connected by explicit vertical edge lines. The graph is a top-down tree structure with proper parent→child lines.
- Athenabox also shows a clearly structured `RUNTIME TREE` box graph.

**Current state:**
- `framebuffer.py:258-290`: The RUNTIME GRAPH draws nodes at offset x/y positions and connects them with diagonal `draw.line()` calls only when a `parent_id` is present. The layout is pseudo-random column offset, not a structured top-down tree with proper branch lines.
- `render/scene.py:362-376`: ANSI version only shows `· label` or vertical `│` with no box drawing.

---

### GAP-09 — TESTING Progress Bar: Pixel-Fill Style with `%` Label — PARTIAL

**What DA GOAL shows:**
- The TESTING view shows a dense filled block bar: `████████████████▒▒▒▒▒▒▒▒ 62%` — a wide, dense bar that spans most of the panel width with a percentage label.
- The bar uses heavy fill blocks `█` for completed and lighter `▒▒` for remaining.

**Current state:**
- `framebuffer.py:427-455`: Renders `[████░░░░░]` using `█` and `░` (light shade, not medium shade `▒`). No `%` label appended.
- `render/scene.py:245-254`: Same issue with `░` instead of `▒` and no `%` suffix.

---

### GAP-10 — RESULT / FAILURE Card: Expected/Actual/Location/Severity Block — PARTIAL

**What DA GOAL shows:**
- The FAILURE scene shows a structured diagnostic card:
  ```
  > RESULT: MISMATCH DETECTED
  expected: handleEdgeCase() == true
  actual:   handleEdgeCase() == false
  at executor.go:142
  severity: medium
  ```
  All fields are left-aligned within the card. The card header `RESULT: MISMATCH DETECTED` is highlighted (amber/red tone).

**Current state:**
- `framebuffer.py:386-409`: Renders `! {location} {message}` per diagnostic. No structured `expected:` / `actual:` / `at:` / `severity:` field decomposition. No `>` chevron prefix on the card header.
- The diagnostic payload keys used are generic (`message`, `detail`, `error`, `path`, `file`, `location`). Fields like `expected`, `actual`, `severity` are not extracted or rendered.

---

### GAP-11 — NEXT STEP / Scanning Animation Row — PARTIAL

**What DA GOAL shows:**
- Below the diagnostic card, a live stream row: `> NEXT STEP: isolate divergence` and `> scanning call graph…` with an animated dot-matrix progress strip at the bottom.

**Current state:**
- The LIVE TRACE band at the bottom renders up to 2 recent alert/stream entries. These are not prefixed with `>` and are not differentiated as "next step" announcements.
- The animated scan beam (motion overlay) exists in `framebuffer.py:617-623` but renders as a horizontal cyan line + small rectangles, not a dot-matrix progress strip matching the reference.

---

### GAP-12 — OI Panel Animation NOT Extended to All Capability/Action Events in ANSI Mode — DEFERRED

**What the reference implies:**
- The scan-beam, pulsing borders, cursor blink, and code-reveal animations should be consistent across all action states. Crucially, the **ANSI renderer** (when Kitty/Glass is unavailable) shows static text and has no animated equivalents for these effects.

**Current state:**
- All animation happens exclusively in `framebuffer.py:574-688` (Glass/Kitty mode only).
- `render/scene.py` (ANSI mode) has no animation pass — no cursor blink, no animated progress indicator, no scan-line sweep indicator.
- The `AnimationClock` callback `dual_pane.py:898-909` short-circuits with `return False` when `display != "glass"`, so animation ticks never trigger ANSI repaints.

---

## Part 3 — Implementation Notes (Exact Code Changes)

### IMPL-01 — Outer Bezel + Nested Aperture Rims
**Target:** `src/athena/cli/dual_pane.py` — `_frame_lines()` method, ~lines 1332–1407

**Goal:** Produce a visual nesting: outer chassis → per-aperture bezel rim → aperture content.

#### Step 1 — Add BEZEL_MARGIN constant to DualPaneSurface
```python
# Add near line 762 (class-level constants):
PANE_GAP = 3
BEZEL_MARGIN = 1     # ← NEW: extra 1-cell relief inside outer chassis box
_REPAINT_INTERVAL = 0.04
```

#### Step 2 — Replace the single box with outer chassis + per-aperture rims
In `_frame_lines()`, replace the existing top/bottom/cabinet_row section (lines ~1379-1392):

```python
# --- CURRENT (lines 1379-1392) ---
# top = prefix + "╭" + "─" * (...) + "╮"
# bottom = prefix + "╰" + "─" * (...) + "╯"
# lines[op_y] = self._fit(top, cols)
# lines[op_y + 1] = self._fit(cabinet_row(left_title, right_title), cols)
# for index in range(op_inner_h): ...
# lines[op_y + op_h - 1] = self._fit(bottom, cols)

# --- REPLACE WITH ---
bezel_m  = self.BEZEL_MARGIN
outer_w  = left_w + len(seam_fill) + right_w + 2 + (bezel_m * 2)
bezel_gutter = " " * bezel_m

# Outer chassis box uses heavy double-line chars for the physical cabinet look:
bezel_top    = prefix + "╔" + "═" * outer_w + "╗"
bezel_bottom = prefix + "╚" + "═" * outer_w + "╝"

def chassis_row(left_value: str, right_value: str) -> str:
    left_panel  = "▐" + self._fit(left_value,  op_inner_w) + "▌"
    right_panel = "▐" + self._fit(right_value, oi_inner_w) + "▌"
    return prefix + "║" + bezel_gutter + left_panel + seam_fill + right_panel + bezel_gutter + "║"

# Per-aperture inner rim rows (top):
left_rim_top  = "╓" + "─" * op_inner_w + "╖"
right_rim_top = "╓" + "─" * oi_inner_w + "╖"
rim_top_row   = prefix + "║" + bezel_gutter + left_rim_top + seam_fill + right_rim_top + bezel_gutter + "║"

# Per-aperture inner rim rows (bottom):
left_rim_bot  = "╙" + "─" * op_inner_w + "╜"
right_rim_bot = "╙" + "─" * oi_inner_w + "╜"
rim_bot_row   = prefix + "║" + bezel_gutter + left_rim_bot + seam_fill + right_rim_bot + bezel_gutter + "║"

lines[op_y]     = self._fit(bezel_top, cols)
lines[op_y + 1] = self._fit(rim_top_row, cols)
lines[op_y + 2] = self._fit(chassis_row(left_title, right_title), cols)

# Content rows — note op_inner_h shrinks by 4 (2 rim rows each end):
content_h = max(op_inner_h - 4, 1)
for index in range(content_h):
    row = op_y + 3 + index
    lines[row] = self._fit(
        chassis_row(
            left[index]  if index < len(left)  else "",
            right[index] if index < len(right) else "",
        ),
        cols,
    )

lines[op_y + 3 + content_h]     = self._fit(rim_bot_row, cols)
lines[op_y + 3 + content_h + 1] = self._fit(bezel_bottom, cols)
```

> **layout.py adjustment** — reduce aperture height by 4 to account for the two extra rim rows at each end:
> ```python
> # layout.py, line 114 — CURRENT:
> aperture_height = max(rows - header_height - rail_height, 1)
> # REPLACE WITH:
> aperture_height = max(rows - header_height - rail_height - 4, 1)
> ```

---

### IMPL-02 — Bottom Hardware Row Redesign
**Target:** `src/athena/cli/dual_pane.py` — `_frame_lines()`, lines 1394–1407

Add class-level glyph constants:
```python
# Near PANE_GAP / BEZEL_MARGIN constants:
_SPEAKER_GRILLE = "▐▌▐▌▐▌▐▌"   # 8-char speaker dot pattern
_KNOB_LARGE     = "( ◎ )"        # 6-char large rotary knob
_KNOB_SMALL     = "(○)"           # 3-char small knob
```

Replace the controls + prompt area (lines ~1394-1407):
```python
controls_y   = layout.controls.y
task_status  = self.projection.status
running_led  = "●" if task_status not in {"READY", "IDLE", ""} else "·"
model_serial = self._fit(self.model_label, 18)
prompt_text  = self._prompt_text or "type a request · /help for controls"

# --- Section A: speaker grille (left) ---
sect_a = self._SPEAKER_GRILLE

# --- Section B: LED status cluster ---
led_block = f"SYSTEM {running_led}  NETWORK ●  ACTIVITY ●"

# --- Section C: large dial ---
sect_c = self._KNOB_LARGE

# --- Section D: right knob cluster + model panel + running LED ---
sect_d = (
    f"BRIGHTNESS {self._KNOB_SMALL}  "
    f"FOCUS {self._KNOB_SMALL}  "
    f"POWER □  "
    f"│ ATHENA / {model_serial} │  "
    f"RUNNING {running_led}"
)

# --- Row layout ---
lines[controls_y]     = self._fit("─" * max(cols - 4, 1), cols)
lines[controls_y + 1] = self._fit(
    f"  {sect_a}   {led_block}   {sect_c}   {sect_d}", cols
)
lines[controls_y + 2] = self._fit(
    f"  AUDIO OUT   {self._fit(prompt_text, 40)}   "
    f"↑↓ scroll  ←→ left/right  Ctrl-C cancel", cols
)
lines[controls_y + 3] = self._fit("─" * max(cols - 4, 1), cols)

prompt_y = layout.prompt.y
if prompt_y < rows:
    lines[prompt_y] = self._fit(f"❯ {prompt_text}    Ctrl-C cancel · Ctrl-D exit", cols)
```

> **layout.py adjustment:**
> ```python
> # layout.py, line 113 — CURRENT:
> rail_height = 4
> # REPLACE WITH:
> rail_height = 5    # +1 for the extra hardware row line
> ```

---

### IMPL-03 — `LIVE VIEWPORT` Label + Header Chip
**Target:** `src/athena/cli/dual_pane.py` — `_frame_lines()`, lines 1360–1367

```python
# CURRENT (line 1363):
right_title = "OI // HISTORY" if self._right_scroll else "ATHENA OI // GLASS COMPUTE"

# REPLACE WITH:
if self._right_scroll:
    right_title = "OI // HISTORY"
else:
    base = "ATHENA OI // GLASS COMPUTE"
    live = "LIVE VIEWPORT"
    pad  = max(oi_inner_w - len(base) - len(live) - 2, 1)
    right_title = base + " " * pad + live

# Also update lines[0] for the chassis-top header chip:
# CURRENT (line 1348):
lines[0] = self._fit("ATHENA  //  OPERATOR INSTRUMENT", cols)
# REPLACE WITH:
chip        = f"[ GLASS COMPUTE ENGINE {'■' if self.display == 'glass' else '□'} ]"
header_body = "ATHENA  OPERATOR INSTRUMENT  //"
chip_pad    = max(cols - len(header_body) - len(chip) - 4, 1)
lines[0]    = self._fit(header_body + " " * chip_pad + chip, cols)
```

---

### IMPL-04 — Dot-Matrix / Pixelated Buddy Style
**Target:** `src/athena/cli/framebuffer.py` — add helpers + modify `_draw_buddy()`

#### Step 1 — Add pixel helpers to `OIFrameBuffer`
```python
# Add after _text() static method (around line 162):

def _pixel_rect(
    self,
    draw: Any,
    x1: int, y1: int, x2: int, y2: int,
    *,
    fill: "Color",
    pixel_size: int = 3,
    density: float = 1.0,
) -> None:
    """Fill a rectangle using discrete pixel blocks (dot-matrix CRT effect)."""
    import random
    rng = random.Random(x1 * 31 + y1 * 97)   # stable per position
    for py in range(y1, y2, pixel_size):
        for px in range(x1, x2, pixel_size):
            if rng.random() < density:
                draw.rectangle(
                    (px, py, px + pixel_size - 1, py + pixel_size - 1),
                    fill=fill,
                )

def _pixel_line(
    self,
    draw: Any,
    x1: int, y1: int, x2: int, y2: int,
    *,
    fill: "Color",
    pixel_size: int = 3,
) -> None:
    """Draw a line using discrete pixel blocks."""
    dist = max(abs(x2 - x1), abs(y2 - y1), 1)
    for i in range(0, dist + pixel_size, pixel_size):
        t  = min(i / dist, 1.0)
        px = int(x1 + (x2 - x1) * t)
        py = int(y1 + (y2 - y1) * t)
        draw.rectangle((px, py, px + pixel_size - 1, py + pixel_size - 1), fill=fill)
```

#### Step 2 — Pixelate the body in `_draw_buddy()`
Replace the smooth `rounded_rectangle` body (lines ~786-801) with:
```python
# CURRENT:
draw.rounded_rectangle(
    (x - body_w, body_top, x + body_w, body_bottom),
    radius=max(2, scale // 2), outline=color, fill=(16, 28, 51, 238), width=stroke,
)
draw.rounded_rectangle(
    (x - scale * 2, screen_top, x + scale * 2, screen_bottom),
    radius=max(1, scale // 4), outline=(100, 148, 191, 180), fill=(9, 20, 39, 245), width=stroke,
)

# REPLACE WITH (pixel = quantization unit):
pixel = max(2, scale // 5)
# Body fill — dithered pixel interior
self._pixel_rect(
    draw, x - body_w + pixel, body_top + pixel, x + body_w - pixel, body_bottom - pixel,
    fill=(16, 28, 51, 230), pixel_size=pixel, density=0.88,
)
# Body outline as pixel chains
self._pixel_line(draw, x - body_w, body_top,    x + body_w, body_top,    fill=color, pixel_size=pixel)
self._pixel_line(draw, x - body_w, body_bottom, x + body_w, body_bottom, fill=color, pixel_size=pixel)
self._pixel_line(draw, x - body_w, body_top,    x - body_w, body_bottom, fill=color, pixel_size=pixel)
self._pixel_line(draw, x + body_w, body_top,    x + body_w, body_bottom, fill=color, pixel_size=pixel)
# Screen face — tighter fill density for phosphor glow look
screen_fill = (9, 20, 39, 245)
self._pixel_rect(
    draw, x - scale * 2 + pixel, screen_top + pixel, x + scale * 2 - pixel, screen_bottom - pixel,
    fill=screen_fill, pixel_size=pixel, density=0.95,
)
self._pixel_line(draw, x - scale*2, screen_top,    x + scale*2, screen_top,    fill=(100, 148, 191, 180), pixel_size=pixel)
self._pixel_line(draw, x - scale*2, screen_bottom, x + scale*2, screen_bottom, fill=(100, 148, 191, 180), pixel_size=pixel)
self._pixel_line(draw, x - scale*2, screen_top,    x - scale*2, screen_bottom, fill=(100, 148, 191, 180), pixel_size=pixel)
self._pixel_line(draw, x + scale*2, screen_top,    x + scale*2, screen_bottom, fill=(100, 148, 191, 180), pixel_size=pixel)
# Eyes — pixel blocks instead of smooth ellipses
for ex in (x - scale*2 + stroke + eye_shift, x + scale - stroke + eye_shift):
    self._pixel_rect(draw, ex, eye_y, ex + scale - stroke, eye_y + eye_height,
                     fill=eye, pixel_size=max(2, pixel - 1), density=1.0)
```

Apply the same pixelation to antenna and feet lines (replace `draw.line()` with `self._pixel_line()`).

---

### IMPL-05 — MODEL REQUEST Row in OI Panel
**Target:** `src/athena/cli/scene.py` — add `model_label` field to `OIScene`
**Target:** `src/athena/cli/projection.py` — add `model_label` attribute
**Target:** `src/athena/cli/framebuffer.py` — `_render_base()` header section
**Target:** `src/athena/cli/render/scene.py` — top of `render_scene_lines()`

#### scene.py
```python
# In OIScene dataclass — add field:
model_label: str = "local/model"

# In build_oi_scene() — pass through:
return OIScene(
    ...
    model_label=getattr(state, "model_label", "local/model"),
)
```

#### projection.py
```python
# In ProjectionState.__init__():
self.model_label: str = "local/model"
```

#### dual_pane.py
```python
# In DualPaneSurface.__init__(), after self.model_label = ...:
self.projection.model_label = self.model_label
```

#### framebuffer.py `_render_base()` — replace lines 195-203:
```python
# CURRENT:
self._text(draw, (margin, top), scene.title, font_small, bright)
self._text(draw, (margin, top + 22),
    f"MODE  {scene.status:<14}  VIEWPORT  {width}×{height}", font_small, dim)
draw.line((margin, top + 43, ...), ...)

# REPLACE WITH (adds MODEL REQUEST and ACTIVE OPERATION rows):
self._text(draw, (margin, top), scene.title, font_small, bright)
model_req = f"> MODEL REQUEST · {scene.model_label}"
self._text(draw, (margin, top + 18), model_req, font_small, accent)
op_label  = "> ACTIVE OPERATION" if scene.mode.value not in {"idle", "respond", "think"} else "> IDLE"
self._text(draw, (margin, top + 36), op_label, font_small, dim)
draw.line((margin, top + 53, width - margin, top + 53), fill=(115, 154, 196, 95), width=1)
# Downstream body_top references shift from (top + 62) to (top + 68):
body_top = top + 68   # was top + 62
```
> Apply `body_top = top + 68` consistently throughout `_render_base()` and `_render_action_content()`.

#### render/scene.py `render_scene_lines()` — insert before `canvas.put(0, 0, "WORKSPACE MAP")`:
```python
# Before the WORKSPACE MAP line (currently line 349):
model_req_text = f"> MODEL REQUEST · {getattr(state, 'model_label', 'local/model')}"
canvas.put(0, 0, model_req_text)
active_text = "> ACTIVE OPERATION" if state.active_operation_id else "> IDLE"
canvas.put(1, 0, active_text)
# WORKSPACE MAP / RUNTIME GRAPH headers shift to row 2:
canvas.put(2, 0, "WORKSPACE MAP")
if split < width:
    canvas.put(2, split, "RUNTIME GRAPH")
# All resource rows shift from enumerate(..., 1) to enumerate(..., 3):
for row, line in enumerate(tree, 3):
    canvas.put(row, 0, line)
# (Also shift runtime_entities loop, active_lines, etc. by +2)
```

---

### IMPL-06 — File Tree with `├─` / `└─` and Right-Aligned Badge Tokens
**Target:** `src/athena/cli/framebuffer.py` — `_render_base()`, lines 243-256
**Target:** `src/athena/cli/render/scene.py` — lines 353-360

#### framebuffer.py — add `_tree_rows()` helper
```python
def _tree_rows(
    self,
    draw: Any,
    entities: list,
    font: Any,
    left: int,
    start_y: int,
    available_width: int,
    row_height: int,
    ink: "Color",
    accent: "Color",
    warn: "Color",
    bad: "Color",
    max_rows: int = 8,
) -> None:
    """Render entities as file-tree rows with ├─/└─ prefix and right badge."""
    n = min(len(entities), max_rows)
    for idx in range(n):
        entity  = entities[idx]
        is_last = idx == n - 1
        prefix  = "└─" if is_last else "├─"
        status  = str(getattr(entity, "status", "")).lower()
        badge   = (
            "[✓] read"    if status in {"complete", "success", "read"}
            else "[!] err"    if status in {"failed", "failure", "error"}
            else "[…] work"   if status in {"active", "running", "requested"}
            else "[·] watch"
        )
        color = self._entity_color(entity, ink, accent, warn, bad)
        max_label = max(1, (available_width // 9) - len(badge) - 4)
        label     = entity.label[:max_label]
        self._text(draw, (left, start_y + idx * row_height),
                   f"{prefix} {label}", font, color)
        badge_x = left + available_width - len(badge) * 7
        self._text(draw, (badge_x, start_y + idx * row_height), badge, font, color)
```

In `_render_base()`, replace the flat tree loop (lines ~253-256):
```python
# REPLACE:
for idx, (label, color) in enumerate(tree):
    self._text(draw, (left, body_top + 24 + idx * 20), label, font, color)

# WITH:
self._tree_rows(
    draw, resources, font_small, left, body_top + 24,
    available_width=width // 2 - margin - 4,
    row_height=20,
    ink=ink, accent=accent, warn=warn, bad=bad,
)
```

#### render/scene.py — replace flat resource loop (lines 356-360, adjusted row numbers after IMPL-05):
```python
# REPLACE:
for row, line in enumerate(tree, 3):
    canvas.put(row, 0, line)

# WITH:
n = min(len(resources), 8)
for idx in range(n):
    entity  = resources[idx]
    is_last = idx == n - 1
    pfx     = "└─" if is_last else "├─"
    status  = str(entity.status).lower()
    badge   = (
        "[✓] read"  if status in {"complete", "success", "read"}
        else "[!]"      if status in {"failed", "failure"}
        else "[●]"      if status in {"active", "running"}
        else "[…]"
    )
    label_w = max(split - 8 - len(badge), 4)
    canvas.put(3 + idx, 0, f"{pfx} {entity.label[:label_w]}")
    canvas.put(3 + idx, split - len(badge), badge)
```

---

### IMPL-07 — Structured Top-Down RUNTIME TREE
**Target:** `src/athena/cli/framebuffer.py` — `_render_base()`, lines 258-290

Replace the pseudo-random node graph with a proper top-down tree:
```python
# REPLACE lines 258-290 WITH:
right_col_x = right + 4
right_w_px  = width - right_col_x - margin
node_w      = min(90, right_w_px - 8)
node_h      = 24
cx          = right_col_x + right_w_px // 2   # horizontal centre
tree_top_y  = body_top + 18

# Build adjacency
adj: dict[str, list] = {}
roots: list = []
for entity in runtime_entities[:6]:
    pid = str(entity.metadata.get("parent_id") or "")
    if pid and any(e.id == pid for e in runtime_entities[:6]):
        adj.setdefault(pid, []).append(entity)
    else:
        roots.append(entity)

def _node(ent, nx, ny):
    ec = self._entity_color(ent, ink, accent, warn, bad)
    draw.rounded_rectangle(
        (nx - node_w // 2, ny - node_h // 2, nx + node_w // 2, ny + node_h // 2),
        radius=3, outline=ec, fill=(18, 32, 58, 210), width=1,
    )
    marker = _state_marker(ent.status)
    self._text(draw, (nx - node_w // 2 + 5, ny - 9),
               f"{ent.label[:14]} {marker}", font_small, ec)
    self._text(draw, (nx - node_w // 2 + 5, ny + 2),
               ent.kind[:10], font_small, dim)

row_y = tree_top_y
for root in roots:
    _node(root, cx, row_y)
    children = adj.get(root.id, [])
    if children:
        draw.line(
            (cx, row_y + node_h // 2, cx, row_y + node_h // 2 + 10),
            fill=(88, 145, 183, 100), width=1,
        )
    step = node_h + 18
    for child in children:
        cy = row_y + step
        _node(child, cx, cy)
        draw.line((cx, row_y + node_h//2 + 10, cx, cy - node_h//2),
                  fill=(88, 145, 183, 100), width=1)
        row_y = cy
    row_y += node_h + 10

if not runtime_entities:
    self._text(draw, (right_col_x, body_top + 42),
               "· no runtime operations observed", font_small, dim)
```

---

### IMPL-08 — Progress Bar: Use `▒` + Percentage Label
**Target:** `src/athena/cli/framebuffer.py` — `_render_action_content()`, TEST block (~line 433)
**Target:** `src/athena/cli/render/scene.py` — TEST block (~line 250)

```python
# framebuffer.py — CURRENT:
"[" + "█" * filled + "░" * (bar_width - filled) + "]"
# REPLACE WITH:
pct = int(value * 100)
"█" * filled + "▒" * (bar_width - filled) + f"  {pct}%"

# render/scene.py — CURRENT:
canvas.put(3, 0, "[" + "█" * filled + "░" * (total_cells - filled) + "]")
# REPLACE WITH:
pct = int(min(max(float(progress["value"]), 0.0), 1.0) * 100)
bar = "█" * filled + "▒" * (total_cells - filled)
canvas.put(3, 0, f"{bar}  {pct}%")
```

---

### IMPL-09 — FAILURE Card: Structured `expected:` / `actual:` / `at:` / `severity:`
**Target:** `src/athena/cli/projection.py` — diagnostic normalization in `reduce()`
**Target:** `src/athena/cli/framebuffer.py` — FAILURE block in `_render_action_content()`
**Target:** `src/athena/cli/render/scene.py` — FAILURE block in `_action_lines()`

#### projection.py — extend diagnostic normalization:
```python
# In reduce(), DiagnosticsProduced handler — expand the normed dict:
for d in payload.get("diagnostics") or []:
    normed = {
        "message":  d.get("message") or d.get("detail") or str(d),
        "path":     d.get("path") or d.get("file") or d.get("location") or "",
        "expected": str(d.get("expected") or ""),
        "actual":   str(d.get("actual")   or ""),
        "severity": str(d.get("severity") or ""),
    }
    self.diagnostics.append(normed)
```

#### framebuffer.py — FAILURE block (replace lines ~386-409):
```python
for diagnostic in scene.diagnostics[: max((height - row - 20) // row_height, 1)]:
    msg      = diagnostic.get("message") or str(diagnostic)
    location = diagnostic.get("path") or diagnostic.get("location") or ""
    expected = diagnostic.get("expected", "")
    actual   = diagnostic.get("actual",   "")
    severity = diagnostic.get("severity", "")

    self._text(draw, (margin, row), f"> RESULT: {msg}", font, bad)
    row += row_height
    if expected:
        self._text(draw, (margin, row), f"expected: {expected}", font, ink)
        row += row_height
    if actual:
        self._text(draw, (margin, row), f"actual:   {actual}", font, ink)
        row += row_height
    if location:
        self._text(draw, (margin, row), f"at {location}", font_small, dim)
        row += row_height
    if severity:
        self._text(draw, (margin, row), f"severity: {severity}", font_small, warn)
        row += row_height
```

#### render/scene.py — FAILURE block (replace lines ~216-229):
```python
row = 2
for diagnostic in scene.diagnostics[: max(height - 4, 1)]:
    msg      = diagnostic.get("message") or diagnostic.get("detail") or str(diagnostic)
    location = diagnostic.get("path") or diagnostic.get("file") or diagnostic.get("location") or ""
    expected = diagnostic.get("expected", "")
    actual   = diagnostic.get("actual",   "")
    severity = diagnostic.get("severity", "")

    canvas.put(row, 0, f"> RESULT: {msg}".strip()); row += 1
    if expected:
        canvas.put(row, 0, f"expected: {expected}"); row += 1
    if actual:
        canvas.put(row, 0, f"actual:   {actual}");   row += 1
    if location:
        canvas.put(row, 0, f"at {location}");         row += 1
    if severity:
        canvas.put(row, 0, f"severity: {severity}");  row += 1
```

---

### IMPL-10 — NEXT STEP Row with `>` Prefix in LIVE TRACE Band
**Target:** `src/athena/cli/projection.py` — add `announce_next_step()` helper
**Target:** `src/athena/cli/framebuffer.py` — LIVE TRACE band in `_render_base()` and `_render_action_content()`

#### projection.py:
```python
# Add method to ProjectionState:
def announce_next_step(self, message: str) -> None:
    """Add a highlighted next-step entry to the live stream."""
    self.feed_stream(f"NEXT STEP: {message}")
    self.add_recent(">", message)
```

Call from `reduce()` when `TaskIterationStarted` fires (and optionally on `CapabilityRequested` transitions):
```python
elif event_type == "TaskIterationStarted":
    iteration = payload.get("iteration", "?")
    self.announce_next_step(f"iteration {iteration}")
```

#### framebuffer.py — update LIVE TRACE band rendering in `_render_base()` (lines ~299-313):
```python
entries = scene.alerts[-3:] or scene.stream[-3:] or ["awaiting canonical events"]
for idx, entry in enumerate(entries):
    is_next  = str(entry).upper().startswith(("NEXT STEP", "SCANNING", "PROPOSING", "CHECKING"))
    prefix   = "> " if is_next else "  "
    color    = (
        bad    if "fail" in entry.lower() or "error" in entry.lower()
        else warn   if "approval" in entry.lower()
        else accent if is_next
        else ink
    )
    self._text(
        draw,
        (margin, band_y + 30 + idx * 18),
        f"{prefix}{entry[: max(20, width // 12)]}",
        font_small, color,
    )
```

---

### IMPL-11 — Enable Animation Ticks in ANSI Mode + Visual Indicators
**Target:** `src/athena/cli/dual_pane.py` — `_animation_tick()`, lines 898-909
**Target:** `src/athena/cli/render/scene.py` — `render_scene_lines()` signature + body
**Target:** `src/athena/cli/dual_pane.py` — `_scene_lines()`, line ~1308

#### dual_pane.py `_animation_tick()`:
```python
# CURRENT:
def _animation_tick(self, dt: float) -> bool:
    if not self._full_screen or self.display != "glass":
        return False
    if not self.animator.tick(dt):
        return False
    self.mascot.advance(dt)
    self.repaint_oi(force=False)
    return True

# REPLACE WITH:
def _animation_tick(self, dt: float) -> bool:
    if not self._full_screen:
        return False
    active = self.animator.tick(dt)
    if not active:
        return False
    self.mascot.advance(dt)
    # Glass: present Kitty frames; ANSI: do a differential text repaint
    self.repaint_oi(force=False)
    return True
```

#### render/scene.py `render_scene_lines()` — add `visual` parameter:
```python
# Change signature:
def render_scene_lines(
    state: ProjectionState,
    scene: OIScene,
    *,
    width: int,
    height: int,
    buddy_lines: Iterable[str] = (),
    buddy_enabled: bool = True,
    recent: Iterable[tuple[str, str]] | None = None,
    visual: "OIVisualState | None" = None,    # NEW
) -> list[str]:
```

After building the canvas (before returning), inject animated indicators:
```python
# At the end of render_scene_lines(), before `return canvas.lines()`:
if visual is not None:
    from athena.cli.activity import VisualActionKind as _VAK
    if scene.mode is _VAK.CODE:
        # Blinking block cursor on the code view
        cursor_visible = visual.cursor_phase < 0.55
        if cursor_visible:
            code_rows = len(scene.code_view.lines) if scene.code_view else 0
            cursor_row = min(height - 1, 4 + code_rows)
            canvas.put(cursor_row, 0, "█", overwrite=True)
    elif scene.mode is _VAK.TEST:
        # Scanning dot on the progress bar row
        scan_pos = int(visual.scan_phase * max(width - 4, 1))
        if 3 < height:
            canvas.put(3, min(scan_pos, width - 2), "◆", overwrite=True)
    elif scene.mode in {_VAK.SEARCH, _VAK.INSPECT, _VAK.READ}:
        # Animated scan cursor in workspace map column
        scan_y = int(visual.scan_phase * max(height // 2, 1))
        if 3 + scan_y < height:
            canvas.put(3 + scan_y, 0, "▶", overwrite=True)
```

#### dual_pane.py `_scene_lines()` — pass visual:
```python
# CURRENT (line ~1308):
return render_scene_lines(
    self.projection, self.scene,
    width=width, height=height,
    buddy_lines=(...), buddy_enabled=self.mascot_enabled,
    recent=self._recent,
)
# REPLACE WITH:
return render_scene_lines(
    self.projection, self.scene,
    width=width, height=height,
    buddy_lines=(...), buddy_enabled=self.mascot_enabled,
    recent=self._recent,
    visual=self.animator.visual,    # ADD
)
```

---

### IMPL-12 — Expand `_ACTIVE_STATES` for Full Coverage
**Target:** `src/athena/cli/animation.py` — `_ACTIVE_STATES`, lines 13-28

```python
# CURRENT:
_ACTIVE_STATES = frozenset({
    "THINKING", "READING", "SEARCHING", "TOOLS",
    "EXECUTING", "APPROVAL", "FAILURE", "RECOVERING",
    "DELEGATED", "CODING", "VERIFYING", "TESTING",
})

# REPLACE WITH:
_ACTIVE_STATES = frozenset({
    "THINKING",
    "READING",
    "SEARCHING",
    "TOOLS",
    "EXECUTING",
    "APPROVAL",
    "FAILURE",
    "RECOVERING",
    "DELEGATED",
    "CODING",
    "VERIFYING",
    "TESTING",
    "RESPONDING",   # Model streaming — cursor blink during response
    "INSPECTING",   # Eye scan animation active
    "GENERATING",   # Tool construction animation
})
```

---

## Part 4 — Implementation Priority Order

| Priority | Gap | Primary Files | Effort |
|---|---|---|---|
| **P1** | IMPL-01 — Outer bezel + aperture rims | `dual_pane.py`, `layout.py` | Medium |
| **P1** | IMPL-03 — LIVE VIEWPORT label + header chip | `dual_pane.py` | Low |
| **P1** | IMPL-02 — Bottom hardware row redesign | `dual_pane.py`, `layout.py` | Medium |
| **P2** | IMPL-05 — MODEL REQUEST row in OI | `framebuffer.py`, `render/scene.py`, `scene.py`, `projection.py` | Medium |
| **P2** | IMPL-06 — File tree with `├─`/`└─` and badges | `framebuffer.py`, `render/scene.py` | Medium |
| **P2** | IMPL-07 — Structured RUNTIME TREE | `framebuffer.py` | Medium |
| **P2** | IMPL-09 — FAILURE card structured fields | `framebuffer.py`, `render/scene.py`, `projection.py` | Medium |
| **P3** | IMPL-04 — Dot-matrix pixel Buddy | `framebuffer.py` | High |
| **P3** | IMPL-08 — Progress bar `▒` + `%` | `framebuffer.py`, `render/scene.py` | Low |
| **P3** | IMPL-10 — NEXT STEP trace rows | `framebuffer.py`, `projection.py` | Low |
| **P3** | IMPL-11 — ANSI animation ticks | `dual_pane.py`, `render/scene.py` | Medium |
| **P3** | IMPL-12 — Expand `_ACTIVE_STATES` | `animation.py` | Low |

---

## Part 5 — Current Release-Truth Note

The historical gaps above should not be read as claims that the current code
still lacks every listed element. The shared scene model now carries the model
request row, bounded workspace/runtime trees, structured diagnostics, and
action-specific progress into ANSI, Glass, and the native bridge. The ANSI
surface intentionally remains event-driven and static between state changes;
continuous animation is limited to the hosted Glass path, with reduced-motion
controls available. Hardware labels that have no telemetry backing are rendered
as application state or unavailable values rather than fabricated sensor data.
