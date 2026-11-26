# Shadow

## Purpose

Own isolated candidate workspaces and commit/discard proof.

## Contract

Shadow branches are explicit, durable, and conflict-aware. A candidate is not
reality until the reality coordinator proves and promotes it.

`ShadowEngine.dispatcher` is the read-only binding projection for composition
and commit readiness; callers must not inspect the private dispatcher field.

## Verification

Run shadow and reality integrity tests.
