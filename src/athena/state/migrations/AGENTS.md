# State Migrations

## Purpose

This directory contains durable schema migrations and their integrity
metadata.

## Ownership

Migrations own schema evolution only; application behavior remains in state
repositories.

## Local Contracts

- Applied migrations are immutable.
- New migrations must be ordered, deterministic, and safe across interrupted runs.
- Migration digests and compatibility fixtures must be updated together.

## Work Guidance

Never rewrite an applied migration to repair current behavior. Add a new
migration and prove upgrade behavior.

## Verification

Run migration and crash tests plus the repository's schema-integrity checks.

## Child DOX Index

No nested contracts are currently required.
