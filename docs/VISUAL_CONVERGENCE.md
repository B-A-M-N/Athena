# Visual Convergence Contract

## Authoritative Visual Target

Athena is intended to combine two visual references:

1. **AthenaBOX**  
   This governs:
   - overall machine/chassis design
   - physical enclosure
   - CRT apertures
   - bezel geometry
   - glass/recess/depth
   - lower hardware controls
   - overall silhouette
   - screen-to-chassis ratio
   - industrial physicality

2. **DAGOAL**  
   This governs:
   - right-side Glass Compute visual language
   - dot-matrix graphics
   - animated computational topology
   - particles/glyphs/grids/traces
   - procedural execution visualization
   - activity/state visualization

The governing rule is:

> **ATHENABOX PROVIDES THE MACHINE. DAGOAL PROVIDES THE COMPUTATIONAL DISPLAY LANGUAGE.**

The canonical reference files are kept in `assets/` as `AthenaBOX.png` and
`DAGOAL.png`. `ui-reference/` may exist as a local compatibility alias, but it
is not a second source of truth. Runtime screenshots and reviewer state under
`.ui-loop/` or `.visual-loop/` are disposable and are intentionally ignored by
Git.

The current Athena UI is NOT a design authority. Do not preserve existing visual choices merely because they already exist.

## Workflow

1. Capture current rendered state.
2. Run macro visual review (visual-convergence-macro / MiniMax M3).
3. Select at most three tightly related implementation objectives.
4. Implement the approved tranche only.
5. Render, test, capture new screenshot.
6. Run micro visual review (visual-convergence-micro / Qwen3.6-35B).
7. Apply only localized corrections; at most three consecutive micro cycles.
8. Re-run macro review periodically to detect structural drift.

## Visual Priority Order

1. overall chassis / silhouette
2. display aperture geometry
3. screen-to-chassis proportions
4. CRT glass / physical depth
5. typography / readability
6. lower hardware instrumentation
7. Glass Compute static composition
8. DAGOAL dot-matrix visual vocabulary
9. semantic compute animation
10. responsive behavior and polish

## Anti-Drift Rules

The UI must not converge toward:

- conventional SaaS dashboard
- nested card layouts
- generic rounded panels
- terminal windows floating inside decorative boxes
- modern component-library controls disguised as retro hardware
- excessive borders consuming screen real estate
- tiny concept-art typography that damages readability
- decorative animation disconnected from actual runtime activity

## Completion Gate

The UI task is complete only when:

1. current rendered screenshot exists
2. current screenshot has been reviewed by Qwen
3. current screenshot has been reviewed by MiniMax
4. MiniMax reports no unresolved structural P0 discrepancy
5. Qwen reports no significant localized regression
6. build/test passes
7. current implementation still follows the visual contract
8. no major drift toward generic dashboard design remains
