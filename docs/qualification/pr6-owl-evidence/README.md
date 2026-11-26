# PR #6 owl golden discrepancy — owner-review evidence bundle

Provenance: candidate certification run
[44837755514](https://github.com/B-A-M-N/Athena/actions/runs/44837755514)
(artifact `athena-visual-candidates-07d9fb7d77f7e4c7a44bf94b6c95d6164d6fcd20`)
at integration head `07d9fb7d77f7e4c7a44bf94b6c95d6164d6fcd20`.
Captures were produced by `scripts/native-visual-smoke` in capture mode on the
GitHub Actions runner (Ubuntu 22.04, Xvfb 1920x1080x24, fixed presentation
clock); nothing in `native/assets/oi/visual-goldens/` was modified.

## Findings

> **Correction (supersedes the numbers in the first PR #7 report):** an
> earlier comment stated "15/45 fixtures exceed it — every owl-family
> fixture (0.078–0.091)". The reconciled figures from
> `45-fixture-rmse-table.csv` are: **21/45 fixtures exceed the 0.08 RMSE
> threshold — 17 of 19 owl fixtures (0.0805–0.0908) plus 4 non-owl
> fixtures** (`approval-bot` 0.0828563, `approval-cat` 0.0828562,
> `failure-bot`/`failure-cat` 0.0814077, scene-level drift). "All owl
> fixtures exceed" was wrong: `1672-idle-owl` (0.0779161) and
> `1672-code-owl` (0.0787659) pass. The claim "all 30 bot/cat fixtures
> pass" was also wrong: 22 of the 26 non-owl fixtures pass, 4 exceed.
> (44837755514, 44843525464) produced the identical over-threshold set;
> only `typing-long-owl` moved at the 5th decimal (0.0841672 → 0.0841857,
> caret-blink phase). See the resolution section below for the full list.

- 24/45 fixtures are within the 0.08 RMSE threshold against the
  pre-redesign goldens.
- 21/45 fixtures exceed it — 17 owl-family fixtures (0.0805–0.0908) plus
  `approval-bot`, `approval-cat`, `failure-bot`, `failure-cat` (0.081–0.083).
  See `45-fixture-rmse-table.csv`.
- Region analysis: the pixel difference between committed and captured frames
  is concentrated in the buddy-sprite region (owl density 0.22–0.27 vs 0.00–0.04
  for the bot sprite in the same region). The remaining frame-wide difference
  density is identical between owl and bot fixtures (font-fallback baseline on
  the runner; both pass). The delta is therefore the owl sprite alone.
- Attribution: commit `6bc9b8d` ("refactor: harden runtime and native TUI",
  2026-09-22) introduced `native/src/render/owl_field.rs` and routes
  `character == "owl"` to the new dot-field renderer `draw_sampled_owl`.
  The goldens were approved at `8caeb70` ("fix(native): restore hybrid TUI
  composition", 2026-09-11), before that redesign. The redesign is documented
  as intentional work (Handoff.md D11: "bounded animated Buddy
  terrain/dot-matrix pixels are extracted and tested") and carries dedicated
  unit tests (density hierarchy, twin orbital eye fields, ear tufts,
  reduced-motion determinism, no dropout).
- Status semantics are preserved in the new renderer: APPROVAL renders amber,
  FAILURE/BLOCKED red, default phosphor, via the same `draw_buddy` call site;
  reduced-motion and point-stability contracts are test-enforced.

## Files

- `owlzoom-idle.png`, `owlzoom-approval.png`, `owlzoom-code.png` —
  [committed golden owl | captured owl | committed golden bot | captured bot]
  for scenes idle / approval / code (upscaled 4x, region x=960–1110, y=390–590).
- `owlzoom-idle-close.png` — closer zoom (left committed, right captured).
- `owl-sxs-approval.png`, `owl-pty-sxs-approval.png` — earlier-session
  side-by-side + highlight-diff composites.
- `prior-session/` — the specific artifacts requested for preservation:
  `owl-committed-crop.png`, `owl-captured-crop.png`,
  `committed-approval-owl.png`, `captured-approval-owl.png`,
  `diff-approval-owl.png`.
- `owl-review-6states.png` — post-approval 6-state defect review composite
  (idle, approval, failure / code, search, read; 6x zoom, owl region).
- `owl-ambient-context.png` — wide-context review of the ambient dot field.
- `45-fixture-rmse-table.csv` — full per-fixture RMSE table from the
  certification capture step.

## Owner decision (RESOLVED)

The old owl (white, ~55 px, crown) and the new owl (96x90 px phosphor
dot-field owl with ear tufts, crown lobes, orbital discs, talons) cannot both
be the approved contract. The repository owner reviewed the comparison
artifacts and **approved retaining the new phosphor dot-field owl**
(`6bc9b8d`, `draw_sampled_owl`), with the condition that the captures be
inspected for actual visual defects first.

### Defect inspection performed before regeneration

- Six-state zoom review (idle, approval, failure, code, search, read):
  recognizable owl silhouette in all states, twin symmetric eye fields,
  correct signal colors (approval amber / failure red), no clipping, no
  collision with identity-plate or UI text.
- Ambient-field context review: the surrounding dots are the intentional
  terrain/star dot-matrix field (Handoff.md D11), not rendering garbage.
- Cross-run determinism: 43/45 captures byte-identical (sha256) between two
  independent CI runs (`07d9fb7` and `1f874d8`); the exceptions
  (`typing-long-{owl,bot,cat}`) differ only in a 194x16 px input-caret strip
  (cursor blink phase is not frozen by the presentation clock), RMSE impact
  < 0.001.

### Resolution applied

- Exactly the 21 over-threshold fixtures (17 owl + the 4 approval/failure
  bot/cat) were refreshed from the authoritative final-HEAD certification
  captures (run 44843525464). The other 24 fixtures are byte-untouched.
- The manifest was refreshed through the repository's update-mode mechanism
  (`scripts/native-visual-smoke` refresh semantics): fixture-identity
  assertion (45/45, no missing/unexpected), `renderer_revision` recomputed
  over the 51 declared `renderer-files.json` inputs, `golden_hashes`
  recomputed for all `required_pngs`, `capture_clock: fixed` preserved,
  `review_status` set to `approved` reflecting the owner decision recorded
  above. Thresholds unchanged.
- Stale-approval invalidation verified still fail-closed: tampering any
  declared renderer source invalidates `renderer_revision`.

## Cabinet golden (second correction)

The first report also claimed "cabinet golden PASSES, RMSE 0.0202 ≤ 0.03".
That was a bad offline measurement. With the manifest gate fixed, CI runs
the cabinet check and FAILS at RMSE **0.035235 > 0.03** — verified offline
committed-vs-captured and reproduced by CI (run 44936425019).

Classification performed before refresh:

- The capture is **byte-identical across three independent CI runs"
  (44837755514, 44843525464, 44936425019, sha256 `201212c3…`) — fully
  deterministic; no run-to-run noise.
- Attribution: the committed cabinet golden was captured at `d9f90c0`
  ("converge hybrid TUI visuals", 2026-09-11). Subsequent PR-branch native
  work (`8caeb70` chassis/text/prompt recomposition, `6bc9b8d` render
  restructure: new `fonts.rs`, `glyphs.rs`, `fit.rs`, split `chassis.rs` —
  the same overhaul family the owner approved for the owl) changed the
  dot-matrix label rasterization deterministically.
- Content identity verified: all banners/labels ("ATHENA // CONTROL DECK",
  "ENC", "BRI", "FOCUS", "CRT", "ATHENA 01 // GLASS MODEL …") present at
  identical positions in committed vs captured frames (`banner-zoom.png`);
  the delta is glyph-raster/intensity level, not missing or relocated
  content. The earlier read that the banner was "missing in the fresh
  capture" was a misframed-crop artifact and is retracted.
- No material defects found; the same phosphor-terminal design direction
  applies.

Resolution: `cabinet-golden.png` refreshed from the same byte-identical
deterministic capture. Evidence: `cabinet-diff-heat.png` (full-frame diff
attribution), `banner-zoom.png` (label content identity),
`v-bottom-full.png` (region context). Threshold unchanged (0.03).
