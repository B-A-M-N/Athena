# Scheduler

## Purpose

Claim due triggers and create canonical tasks.

## Contract

Scheduler owns trigger timing and occurrence idempotency only. It never invokes
the model loop, bypasses admission, or fabricates task results.

## Verification

Run scheduler and task-admission tests plus architecture lint.
