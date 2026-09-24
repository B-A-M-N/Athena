# Skills

## Purpose

This package owns durable skill content, selection, validation, candidate review, and lifecycle state.

## Authority And Evidence Boundaries

- `SkillLifecycle` owns durable state transitions and explicit promotion; loaded content does not authorize execution.
- Promotion requires validation and evidence-linked provenance; bundled, project, and user precedence is deterministic.
- The capability and context layers consume `SkillStore`; they must not mutate lifecycle state through private back doors.
- `SkillLoader.refresh()` owns file-backed snapshot replacement and unchanged-version conflict detection; `SkillStore.refresh_file_backed()` reconciles only new versions into lifecycle state. Active task context remains immutable for the current turn.
- `SkillLifecycle` resolves cited candidate evidence against durable task/event records before promotion; invented or missing references remain unproven and fail closed.
- A refinement that widens scope requires multiple resolved observations across at least two observed environments; it cannot silently promote project evidence to user scope.
- Triggering emits exact `SkillApplied` ID/version evidence; context selection alone is availability, not successful use or failure.

## Verification

- Run skills lifecycle/loader/selector tests plus `./scripts/architecture-lint --quiet`.
