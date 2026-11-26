# Native Rendering

## Purpose

Rendering turns the authoritative projection into readable terminal or pixel
presentation.

## Ownership

Render modules own layout, animation, scene composition, text, and hit-test
geometry. They do not own task, model, capability, verification, or failure
semantics.

## Local Contracts

- Consume projection truth; do not reclassify backend events into new semantic truth.
- Presentation changes must preserve readable status and diagnostic information.
- Split large render modules by target, layout, scene, telemetry, attention, and semantic snapshot concerns.

## Work Guidance

Keep platform FFI out of renderers and avoid string-matching backend statuses
when the bridge can carry typed failure data.

## Verification

Run native tests and semantic/visual golden checks where applicable.

## Child DOX Index

| Path | Purpose |
|------|---------|
| `oi/` | Logical OI target, layout, motion, attention, scene, and telemetry boundaries |
| `oi/mod.rs` | Composition-only module boundary and narrow re-exports |
| `oi/target.rs` | OI framebuffer, texture, and target-owned buddy position integration |
| `oi/layout.rs` | Safe areas, scene layout, workspace-tree layout, and runtime-graph geometry |
| `oi/scenes.rs` | Idle, workspace, read, code, execute, test, approval, failure, success, and think scenes |
| `oi/attention.rs` | Attention actions, hit maps, paging, rail, and approval buttons |
| `oi/motion.rs` | Buddy targets, clearance, easing, and packet motion |
| `oi/telemetry.rs` | Operation telemetry and semantic progress rendering |
| `oi/legacy_scene.rs` | Low-level composition pass: CRT treatment, chrome, terrain, and shared pixel primitives |
| `fonts.rs` | Xft font-face acquisition, metrics, scale reconfiguration, and teardown; subordinate to text rendering |
| `color_cache.rs` | Bounded Xft color allocation, reuse, and teardown owned by the text surface |
| `text_cache.rs` | Bounded text-width cache lifecycle and scale-reset behavior |
