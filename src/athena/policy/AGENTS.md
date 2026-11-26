# Policy

## Purpose

Policy owns authorization, effect classification, scopes, budgets, and
approval requirements.

## Ownership

Policy decides whether a requested effect is allowed. It never performs the
effect and never chooses the next reasoning action.

## Local Contracts

- Fail closed when authorization or approval evidence is missing.
- Policy decisions must be inspectable and tied to the request/principal/task.
- Capability and execution layers must not bypass policy for convenience.

## Work Guidance

Keep authorization separate from capability invocation and process execution.

## Verification

Run policy and security tests for every authorization-path change.

## Child DOX Index

No nested contracts are currently required.
