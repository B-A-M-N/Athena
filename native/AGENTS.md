# Native Frontend

## Purpose

The Rust native frontend presents Athena's serialized projection and owns the
terminal/window/input mechanics needed by the certified desktop cell.

## Ownership

Native owns presentation behavior, platform integration, and local rendering
state. Python owns application truth, task semantics, policy, and execution.

## Local Contracts

- The native process deserializes the versioned projection bridge; it does not re-decide backend truth.
- Renderers display projection state and may derive layout, animation, and hit-test geometry only.
- Platform code owns OS/window/input mechanics, not product semantics or task authority.
- Bridge/schema changes require producer-consumer seam evidence.

## Work Guidance

Extract helpers before expanding `main.rs`, `x11.rs`, or the main renderers.
Keep feature-specific scene code out of platform modules and keep platform FFI
out of semantic render modules.

## Verification

- `cargo check --manifest-path native/Cargo.toml --offline`
- `cargo build --manifest-path native/Cargo.toml --locked --offline`
- `cargo test --manifest-path native/Cargo.toml --locked --offline`
- `./scripts/architecture-lint --quiet`

## Child DOX Index

| Path | Purpose |
|------|---------|
| `src/` | Native executable, rendering, and platform modules |
