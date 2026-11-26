# Workflows

## Purpose

Persist and execute deterministic compositions of existing capabilities.

## Contract

Workflow graphs are data and execution plans, not a second agent loop. Policy,
dispatch, budget, workspace, and receipts remain injected authorities.

## Verification

Run workflow store/executor tests and the architecture lint.

## Child DOX Index

| Path | Contract |
|------|----------|
| `receipts.py` | Normalized step-item receipt storage and legacy backfill; subordinate to `WorkflowRunStore`. |
| `identity.py` | Immutable run identity codec and nested-run key derivation; subordinate to `WorkflowRunStore`. |
| `reconciliation.py` | Receipt-bound external-effect recovery transaction; subordinate to `WorkflowRunStore`. |
| `bootstrap.py` | Workflow start/resume identity validation and durable recovery reconstruction; subordinate to `WorkflowRunStore`. |
| `continuations.py` | Approved continuation completion and nested-run wake-up mechanics; subordinate to `WorkflowRunStore`. |
| `step_transactions.py` | Receipt-backed nested-run binding and step-item state transitions; subordinate to `WorkflowRunStore`. |
| `preparation.py` | Stable pre-dispatch step/call identity and durable preparation transaction; subordinate to `WorkflowRunStore`. |
| `status.py` | Run-level status, output, timestamp, and workspace-baseline persistence; subordinate to `WorkflowRunStore`. |
| `queries.py` | Read-only complete run projection and receipt decoding; subordinate to `WorkflowRunStore`. |
