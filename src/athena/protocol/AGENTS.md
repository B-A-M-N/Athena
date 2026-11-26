# Protocol Contracts

## Purpose

This package defines the stable messages, events, task, model, capability,
execution, policy, failure, and error contracts shared by Athena subsystems.

## Ownership

Protocol owns shapes, validation, compatibility, and neutral semantics. It
does not own capability registries, providers, service configuration, kernel
decisions, or execution implementations.

## Local Contracts

- Imports must remain implementation-independent; protocol may depend on the standard library and protocol modules only.
- Effects and capability metadata are supplied to descriptors; protocol objects must not discover them by importing implementations.
- Versioned contracts require compatibility tests and explicit migration behavior.

## Work Guidance

Keep predicates neutral and reusable. Move implementation-specific lookups
downward or sideways rather than adding a deferred import.

## Verification

Run protocol unit/contract tests and `./scripts/architecture-lint --quiet`.

## Child DOX Index

| Path | Purpose |
|------|---------|
| `failure.py` | Typed, implementation-neutral failure envelope |
| `continuations.py` | Approval/input-paused continuation DTOs |
| `capabilities.py` | Capability descriptors, requests, executors, and inventory protocols |
| `affordances.py` | Neutral affordance scopes and dependency requirements |
| `context.py` | Neutral attached-context values and digest store port |
| `reality.py` | Neutral execution disposition vocabulary |
| `workflows.py` | Neutral declared-workflow execution port and result |
| `scheduling.py` | Neutral trigger value objects and next-fire computation |
| `termination.py` | Shared terminal-decision DTO |
| `response_accumulator.py` | Provider-stream mixed-content assembly and bounded ingestion; neutral protocol mechanism. |
| `response_limits.py` | Provider-stream payload accounting and content identity helpers. |
| `hermes.py` | Neutral Hermes supervision policy vocabulary |
