# PR #6 owl golden discrepancy — owner-review evidence bundle

Provenance: candidate certification run
[44837755514](https://github.com/B-A-M-N/Athena/actions/runs/44837755514)
(artifact `athena-visual-candidates-07d9fb7d77f7e4c7a44bf94b6c95d6164d6fcd20`)
at integration head `07d9fb7d77f7e4c7a44bf94b6c95d6164d6fcd20`.
Captures were produced by `scripts/native-visual-smoke` in capture mode on the
GitHub Actions runner (Ubuntu 22.04, Xvfb 1920x1080x24, fixed presentation
clock); nothing in `native/assets/oi/visual-goldens/` was modified.

## Findings

- 30/45 fixtures (all `bot` and `cat`) are within the 0.08 RMSE threshold
  (range 0.069–0.079).
- 15/45 fixtures exceed it — **every owl-family fixture** (0.078–0.091),
  across all scenes. See `45-fixture-rmse-table.csv`.
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
- `45-fixture-rmse-table.csv` — full per-fixture RMSE table from the
  certification capture step.

## Owner decision

The old owl (white, ~55 px, crown) and the new owl (96x90 px phosphor
dot-field owl with ear tufts, crown lobes, orbital discs, talons) cannot both
be the approved contract. No visual gate failure is resolvable by engineering
correction: the renderer change is intentional, complete, tested, and
digest-tracked; the committed goldens are simply stale relative to it.
Outcome of a decision to adopt the new owl design: regenerate the 15 owl
fixtures via `scripts/native-visual-smoke` refresh (fixture-identity asserts
already enforced) and mark approval — owner-only. Declining reverts the owl
renderer to the pre-`6bc9b8d` sprite. Until then the
`core-release-certification` visual gate fails closed by design.
