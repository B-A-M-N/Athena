# Tasks

## Purpose

Tasks own durable task identity, lifecycle transitions, leases, cancellation,
budgets, and result persistence.

## Ownership

Task managers persist and validate lifecycle state. They do not reason, select
providers, authorize effects, or execute processes.

## Local Contracts

- Lifecycle transitions are explicit, durable, and idempotent where required.
- Cancellation and lease loss must be observable through canonical events.
- Task codecs remain protocol-compatible and must not depend on kernel internals.

## Work Guidance

Keep persistence interactions transactional and leave autonomous choices to the
kernel.

## Verification

Run task and state unit tests plus crash/recovery tests for lifecycle changes.

## Child DOX Index

No nested contracts are currently required.
