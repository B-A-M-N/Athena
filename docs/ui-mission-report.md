# Athena UI/UX Mission — Completion Report (Sandboxed Cloud Run)

Date: 2026-08-27. Environment: offline Linux sandbox, Python 3.11, stdlib only
(no `uv`, no pytest, no network; `httpx`/`aiosqlite`/`jsonschema` absent).
The Athena CLI layer is pure-stdlib ANSI rendering, so every change below was
implemented **and** executed in-sandbox. Nothing in this report claims
real-terminal, real-provider, or real-machine validation.

---

## 1. Issues Found (ranked)

### P0 — Interaction correctness

1. **Stderr partial-line corruption in the dual pane.**
   `dual_pane.render_event` split `StderrChunk` on `.splitlines()` and fed
   each line with its own `[err]` prefix. A fragment split across chunks
   (e.g. `parti` + `al fragment\n`) was rendered as two broken, mis-prefixed
   lines. The standalone `oi_stream` viewer had rejoin logic; the dual pane
   (the main CLI surface) did not. *Fixed in `cli/stream.py` — fragments
   rejoin across chunks for both streams; covered by
   `test_stream.py::test_stderr_partial_rejoin_and_flagging` and
   `test_dual_pane_routing.py::test_stderr_routes_to_stream_flagged_not_doubled`.*

2. **Model text concatenated with shell commands in the OI stream.**
   Unterminated model deltas stayed in the partial tail; a following
   `$ command` line appended onto the same visual line
   (`"Inspecting the repository now.$ git status"`). *Fixed by sealing the
   partial before `ExecutionStarted`/`CapabilityRequested` stream markers.*

3. **Carriage-return progress spam.** `\r`-updating progress output
   (download bars, scan counters) appended one committed row per repaint in
   the old `_OIWindow`. *Fixed: CR semantics implemented faithfully in
   `StreamWindow` (column-0 overwrite incl. longer-tail preservation and
   CR-split-across-chunks); `test_stream.py` covers all three cases.*

4. **Chunk-split ANSI escapes could corrupt the display.** An escape
   sequence cut by a chunk boundary (`...\x1b` | `[31m...`) leaked bytes.
   *Fixed with a held-escape buffer (`_esc_hold`) in `StreamWindow`.*

### P1 — Operator comprehension

5. **OI pane was an event firehose with duplicated rows.** Every lifecycle
   event (`CapabilityStarted`, progress, exit) produced its own log line in
   the left pane, and raw stdout/stderr chunks were mirrored **both** to the
   calm pane (via `OperatorSurface._flush_stream`) and to the OI window.
   *Fixed: structured `ActivityModel` updates ONE operation record in place
   across its lifecycle; raw chunks are exclusive to the machine pane unless
   `details=True`. Verified by
   `test_activity.py::test_operation_updated_in_place_not_duplicated` and
   `test_dual_pane_routing.py::test_high_volume_output_bounded_and_chat_readable`
   (5000-line process: calm pane receives zero rows, machine history stays
   bounded at 500 lines with a drop counter).*

6. **"What is Athena doing now?" was not answerable.** No distinction
   between active operation, recent activity, and history. *Fixed: the
   chassis renders ACTIVE OPERATION (with output tail), AUTHORIZATION,
   OUTPUT viewport, and RECENT ACTIVITY as separate insets, plus a status
   strip (`[STATE] current-label`).*

7. **Background/delegated work invisible.** `ChildTaskCreated/Completed`
   had no projection. *Fixed: `ActivityModel.background` with
   completion/failure and `needs_attention` surfacing in the status strip.*

### P2 — Buddy integration

8. **Flat mascot if-chain: no priority, could get stuck, terminal states
   never decayed.** `Mascot.observe` let any later event overwrite state;
   `done`/`failed` persisted forever; there was no approval pinning (stream
   chatter could visually cancel an approval). *Fixed: `cli/buddy.py`
   implements 11 semantic states with an explicit priority table
   (approval > failure/interrupted > recovering > waiting > executing >
   reading > thinking > listening > idle), pinned approval/recovering states
   with explicit exit events, and sticky terminal states that decay to idle
   after a bounded tick count. 21 tests in `test_buddy.py`, including a
   parametrized "never stuck after any terminal lifecycle" proof.*

9. **Low-resolution buddy.** The source art is small ASCII (intrinsic asset
   limitation — the sandbox cannot author production sprite art). *Addressed
   per mission §9: one compact frame per semantic state in
   `dual_pane.BUDDY_ART` (drop-in replaceable without touching the state
   machine), classic multi-frame sets extracted verbatim to
   `cli/mascot_art.py`, fixed-width buddy column inside the chassis.
   Final artwork replacement is an external asset task (see §8).*

### P3 — Interaction polish / visual identity

10. **No machine identity for the OI pane.** The old right pane was a thin
    `┌─ OI · live` box. *Fixed: `cli/chassis.py` — double-line retro
    operator-console chassis with title plate, status strip, inset sections,
    and a deterministic degradation ladder (decoration drops before
    content), tested at 8 size combinations plus degenerate 1×1/0×0.*

11. **Fragmented styling.** *Fixed: `cli/style.py` — one glyph/box/color
    module shared by both panes, with TERM=dumb/NO_COLOR ASCII fallback.*

---

## 2. Changes Made (by file)

| File | Change | Behavioural impact |
|---|---|---|
| `cli/buddy.py` (new) | Semantic state machine | Derived, prioritized, never-stuck buddy state |
| `cli/activity.py` (new) | Structured op registry | One row per operation; sections; approval/artifact/bg projections |
| `cli/stream.py` (new) | `StreamWindow` | CR progress, partial rejoin, ANSI hygiene, bounded history + drop count |
| `cli/chassis.py` (new) | Pure chassis renderer | Retro machine frame; responsive degradation |
| `cli/style.py` (new) | Design-system primitives | Shared glyphs/borders/colors; dumb-terminal fallback |
| `cli/mascot_art.py` (new) | Legacy frame sets | Extracted verbatim; `oi_stream` viewer unchanged |
| `cli/dual_pane.py` | Rewired | New projections; `Mascot`/`_OIWindow` kept as back-compat adapters; raw chunks exclusive to right pane; partial-seal before `$` markers |
| `cli/surface.py`, `cli/chat.py`, `cli/oi_stream.py` | **Untouched** | Base calm surface, REPL, standalone viewer behave as before |
| `scripts/dev_pytest.py` (new) | pytest-subset runner | Makes the suite executable in offline sandboxes |
| `tests/unit/cli/test_{buddy,activity,stream,chassis,dual_pane_routing,golden_scenario}.py` (new) | 81 tests | See §5 |

No kernel, policy, execution, persistence, or session code was modified.
No second authority of any kind was introduced: `Buddy` and `ActivityModel`
are pure read-only projections of the canonical event stream (INV-007).

---

## 3. Pane Ownership Map

| Athena event | Projection | Presentation |
|---|---|---|
| `ModelDelta` / `ModelResponseCompleted` | `OperatorSurface` coalescing + stream tail | LEFT (prose) + RIGHT (raw tail) |
| `CapabilityRequested/Started/Progress` | `ActivityModel` op (in place) + stream `$` line | RIGHT; LEFT gets the one capability card |
| `StdoutChunk` / `StderrChunk` | `StreamWindow` (err flag) + op output tail | RIGHT only (LEFT only with `details=True`) |
| `CapabilityCompleted/Failed` | op → DONE/FAILED, retired to RECENT | RIGHT (card line on LEFT) |
| `ExecutionStarted/Exited/TimedOut/Interrupted` | op state + stream marker | RIGHT (status line on LEFT) |
| `ApprovalRequested` | `PendingApproval` + op → WAITING + buddy → APPROVAL | RIGHT inset AUTHORIZATION card (context preserved) + LEFT approval card |
| `ApprovalResolved` | op resume/cancel + card cleanup | shared (both panes update, card removed) |
| `PolicyDecisionMade` | op detail note | RIGHT |
| `ArtifactCreated` | `ArtifactNote` + op flag | RIGHT RECENT inset + LEFT one-liner |
| `ChildTaskCreated/Completed` | `BackgroundTask` + attention flag | RIGHT (status strip + inset) |
| `TaskStarted/StateChanged/Blocked` | task_status / op pause-resume | RIGHT status strip (LEFT: details only) |
| `TaskCompleted/Partial/Failed/Cancelled/Interrupted` | task_status + retire op + buddy terminal state | shared (LEFT outcome line; RIGHT frame state) |
| `ModelRequestStarted/ReasoningDelta`, `Context*`, `Memory*`, `SkillActivated` | buddy (thinking/reading) | RIGHT (buddy only — no log spam) |
| Mutation/memory/skill audit events | unchanged service projections (`/diff`, `/permissions`) | LEFT via meta-commands |

---

## 4. Buddy State Map

| Athena event(s) | Derived state | Visual | Exit condition |
|---|---|---|---|
| `TaskQueued/Created` | `listening` | owl, dim | any activity event |
| `ModelRequestStarted/ModelDelta/ModelReasoningDelta`, `TaskStarted` | `thinking` | owl + thought glyphs | execution/approval/terminal event |
| `ContextBuild*/Built/Compressed`, `Memory*`, `SkillActivated` | `reading` | owl + book | higher-priority event |
| `CapabilityRequested/Started/Progress`, `Execution*`, `Stdout/StderrChunk`, `MutationRecorded` | `executing` | robot box + run marks | approval / terminal / capability-complete |
| `ChildTaskCreated/Completed`, `TaskBlocked` | `waiting` | owl paused | foreground event / terminal |
| `TaskStateChanged(RECOVERING/RETRYING/RESUMING)` | `recovering` | owl + ↻ | running/terminal event |
| `ApprovalRequested`, `TaskStateChanged(WAITING_APPROVAL)`, `PolicyDecisionMade` | `approval` (**pinned**) | owl + `[?]` "may i?" | `ApprovalResolved` only (stream chatter cannot unpin); on resolve → `executing` if an op is open else `idle` |
| `TaskCompleted/Partial` | `success` (sticky 6 ticks) | ★ owl + ✓ | tick decay → `idle` |
| `TaskFailed`, `CapabilityFailed`, `ExecutionTimedOut`, `ModelRequestFailed` | `failure` (sticky 10) | ✕ owl | tick decay → `idle` |
| `TaskCancelled/Interrupted` | `interrupted` (sticky 10) | ⊘ owl | tick decay → `idle` |
| (none) | `idle` | resting owl | any mapped event |

Priority: `failure/interrupted(9) > success(8) > approval(7) > recovering(6)
> waiting(5) > executing(4) > reading(3) > thinking(2) > listening(1) > idle(0)`.
Lower-priority signals never override a higher-priority visible state; sticky
states swallow chatter until decay.

---

## 5. Tests Added (93 total in `tests/unit/cli`, 81 new)

* **`test_buddy.py` (21)** — event→state mapping for all 11 states; approval
  overrides executing; approval pinned against stream chatter; pinned exit
  resumes the open operation (or idle); sticky success/failure decay to
  idle; sticky states swallow low-priority chatter; cancel→interrupted;
  delegated→waiting; context→reading; recovering; unknown events ignored;
  reset; **parametrized no-stuck-states proof over all terminal lifecycles**.
* **`test_activity.py` (14)** — one operation updated in place (no
  duplication); active visibility; failure reasons; stderr flagging; bounded
  output tail + drop counter; approval card creation + op pause;
  approve-resume / deny-cancel; artifacts with producer; background
  lifecycle + attention; task-terminal retirement; **concurrent operations
  keyed by call_id**; deterministic reset; idle label.
* **`test_stream.py` (15)** — partial rejoin (stdout and stderr); CR
  progress in-place / longer-tail / split-across-chunks; ANSI strip;
  binary-byte replacement; delta partial never duplicates (P2-44 guard);
  ring bound + drop count; snapshot purity; display-only truncation;
  huge-output bounding (10k lines); empty/delayed output.
* **`test_chassis.py` (9)** — chassis sections present; exact height and
  rectangularity; status strip answers current activity; approval card
  visible with context preserved; decoration drops before content at small
  heights; buddy column hides at narrow widths; degenerate sizes never
  crash; parametrized exact-height at 4 sizes; idle machine not blank;
  dropped counter in status.
* **`test_dual_pane_routing.py` (14)** — the pane ownership map: prose left,
  raw chunks right-exclusive; single-op lifecycle; approval placement,
  pause, cleanup; artifact discoverability; background visibility without
  domination; 5k-line bounding; CR progress; small/large terminal layouts;
  `Mascot`/`_OIWindow` back-compat; non-tty repaint safety; failure
  fan-out; **realistic buddy progression script**.
* **`test_golden_scenario.py` (1)** — full scripted task (conversation +
  execution + progress + stderr + approval + artifact + completion)
  asserted semantically across both panes.

## 6. Commands Executed

```
PYTHONPATH=src python3 scripts/dev_pytest.py -q tests/unit/cli     # 93 passed
PYTHONPATH=src python3 scripts/dev_pytest.py -q tests/unit          # 182 passed, 68 failed*
```
\* The 68 failures are byte-identical before and after this mission
(diff-verified): all are `ModuleNotFoundError` for third-party packages
(`jsonschema`, `httpx`, `aiosqlite`, `pexpect`) that the offline sandbox
cannot install — pre-existing environment gaps, not regressions.

## 7. Cloud-Verified Scenario Matrix (§34)

| # | Scenario | Result |
|---|---|---|
| 1 | Application startup | PARTIAL — surface construction + render tested; full `athena` CLI boot needs `httpx`/`aiosqlite` (absent) |
| 2 | Normal conversation (mock provider) | PASS — delta coalescing via scripted events |
| 3 | Multi-turn history | PARTIAL — projection tested; durable history needs `aiosqlite` |
| 4 | Simulated tool execution | PASS |
| 5 | Streamed stdout | PASS (incl. partial lines, bounding) |
| 6 | Streamed stderr | PASS (flagged, rejoined, separate) |
| 7 | High-volume output | PASS (5k/10k-line tests) |
| 8 | Process failure | PASS (op failure + buddy failure + err stream) |
| 9 | Cancellation | PASS (interrupted state, decay) |
| 10 | Approval requested | PASS (inset card, paused op, buddy approval) |
| 11 | Approval accepted | PASS (resume) |
| 12 | Approval denied | PASS (op cancelled, "denied") |
| 13 | Repeated approvals | PASS (handled-set prevents double prompt — pre-existing `OperatorSurface` logic, still green) |
| 14 | Artifact event | PASS |
| 15 | Background task event | PASS |
| 16 | Delegated task event | PASS |
| 17 | High-volume OI while chat readable | PASS |
| 18 | Resize simulations | PASS (80–160 cols via patched `_terminal_size`) |
| 19 | Small dimensions | PASS (28→100 cols; degenerate 1×1/0×0) |
| 20 | Interrupted/recovery state | PASS (interrupted + recovering states) |
| 21 | Mascot transitions through major states | PASS (scripted progression test) |
| — | PTY-driven interactive TUI test | NOT CLOUD-VERIFIABLE — `pexpect` is not installed and cannot be (no network); the CLI's interactivity is `input()`-line-based, so there is additionally no full-screen TUI loop to drive headlessly. Substituted: deterministic view-model tests + non-tty render-path tests. |

## 8. Remaining UI/UX Defects

1. **Buddy artwork resolution** — intrinsic to the source ASCII assets. The
   pipeline is now drop-in replaceable (`BUDDY_ART` per-state frames; art
   sizing is independent of the state machine), but production-quality
   high-resolution art (or Kitty/iTerm/Sixel sprites) is an external asset
   task. Not faked here.
2. **Left pane has no independent scrollback/navigation** — it remains a
   linear terminal stream. True per-pane scrolling requires a full-screen
   TUI framework (none in the dependency set; `pyte` is a terminal emulator
   for the execution sandbox, not a widget framework). Architectural note,
   not something this layer can bolt on without a framework decision.
3. **`oi_stream` viewer full-screen redraw** — repaints the whole frame per
   event (`\x1b[2J`). Fine at terminal sizes but flickers on slow links;
   kept as-is (separate optional viewer), main surface uses cursor
   save/restore in-place repaints.
4. **Direct `!cmd` escapes bypass the activity model** — they render through
   the legacy card path (left) + stream (right) but don't create an
   `Operation`. Cosmetic; the buddy and stream already react.

## 9. Architectural Blockers

1. **Per-pane scrolling / mouse / focus management.** Symptom: no PageUp
   history navigation per pane. Root cause: the CLI is a line-oriented ANSI
   writer with no screen model; there is no TUI framework in
   `pyproject.toml`. Affected: `cli/surface.py`, `cli/dual_pane.py`.
   Invariant: INV-007 (surfaces are projections). Smallest follow-up: adopt
   (or vendor a minimal) screen-buffer abstraction behind a new
   `SurfaceDriver` protocol; the view-models built in this mission
   (`ActivityModel`, `StreamWindow`, `ChassisView`) are already pure and
   carry over unchanged.
2. **Terminal image protocols for the buddy.** Root cause: stdlib-only
   renderer; no Kitty/iTerm/Sixel encoder. Follow-up: a capability-detected
   `ImageBackend` behind the buddy column; `ChassisView.buddy_lines` is the
   seam.

## 10. Real-Machine Validation Manifest

Only items genuinely requiring the user's environment:

1. **Live repaint geometry.** Run `athena chat` in a ≥120-col terminal;
   submit a task that executes a long-running command with `\r` progress
   (e.g. a build). Expected: the machine chassis repaints in place at the
   bottom; progress stays on one line; left pane stays readable. Failure
   symptoms: ghost borders, duplicated rows, cursor left inside the frame.
2. **Resize during streaming.** While (1) runs, shrink the window below 100
   cols, then restore. Expected: chassis collapses to single-column tagged
   output, then reappears; no corruption. Failure: stale frame fragments,
   crash on SIGWINCH.
3. **Real approval interaction.** With autonomy=supervised, trigger a
   policy-gated capability. Expected: AUTHORIZATION inset appears in the
   machine with surrounding context; the left-pane scope selector still
   prompts; after answering, the card disappears and the operation resumes.
   Failure: card stuck, double prompt, lost composed input.
4. **Dumb-terminal fallback.** `TERM=dumb athena chat`. Expected: ASCII
   borders/glyphs, no color escapes, no line wrapping corruption.
5. **Live provider streaming.** With a real provider configured, confirm
   model prose lands in the left pane while the stream viewport shows the
   raw tail — and that non-streaming providers still surface the final
   answer (the `oi_stream` viewer's `get_result` fallback path is unchanged
   but was not cloud-exercised).
6. **Buddy legibility.** Check the buddy column at your font size: glyphs
   like `⊙‿⊙`/`▄▄▄` must not be width-2 surprises in your terminal. If your
   terminal renders them double-width, set `ATHENA_MASCOT=bot` (simplest
   art) or TERM=dumb.

## 11. Operator Walkthrough

**What you can rely on now (cloud-verified):** Submit a task and the left
pane shows only the conversation — Athena's prose, capability cards,
outcome lines. The right machine answers at a glance: a status strip
(`[EXECUTING] execute · rg athena`), an ACTIVE OPERATION inset with the
current command and its last output lines, a bounded OUTPUT viewport with
the buddy reacting in its own column, and RECENT ACTIVITY with completed
operations and artifacts. A 5000-line build floods neither pane: the
viewport shows the tail, the status strip shows `-N` dropped lines, the
conversation stays clean. When policy pauses Athena, an AUTHORIZATION inset
appears inside the machine — the requested capability, the reason, the
scope choices — while the paused operation stays visible; approving resumes
it, denying cancels it with "denied" recorded, and the card never sticks.
The buddy moves through listening → thinking → executing → approval →
success/failure with deterministic exits, and can never wedge in a terminal
state. Small terminals degrade gracefully: buddy column hides first, then
sections drop, then the frame itself — content is never sacrificed to
decoration.

**What still needs your machine (§10):** in-place repaint behaviour under a
real tty, real resize storms, real approval keystrokes, dumb-terminal
fallback, live provider streaming, and glyph-width legibility.
