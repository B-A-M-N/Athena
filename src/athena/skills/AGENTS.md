# Skills

## Purpose

This package owns durable skill content, selection, validation, candidate review, and lifecycle state.

## Authority And Evidence Boundaries

- `SkillLifecycle` owns durable state transitions and explicit promotion; loaded content does not authorize execution.
- Promotion requires validation and evidence-linked provenance; bundled, project, and user precedence is deterministic.
- The capability and context layers consume `SkillStore`; they must not mutate lifecycle state through private back doors.

## Verification

- Run skills lifecycle/loader/selector tests plus `./scripts/architecture-lint --quiet`.
