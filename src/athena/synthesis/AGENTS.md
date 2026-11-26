# Synthesis

## Purpose

Synthesis turns verified, durable evidence into generated capability records
and proof-carrying promotion decisions.

## Ownership

`engine.py` coordinates synthesis requests; `validation.py` owns generated
discipline; `proof_ledger.py` owns evidence/proof records; `promotion.py`
owns lifecycle promotion; and `runtime.py`/`child_runtime.py` execute only
validated generated records. Synthesis must not become a second task loop or
silently bypass verification.

## Local Contracts

- Generated records remain reproducible, scoped, and tied to their dependency lock.
- Promotion requires the durable verification/proof contract.
- Runtime execution re-enters canonical capability dispatch and preserves failure semantics.

## Verification

Run synthesis capability tests, generated-discipline tests, Ruff, and
`./scripts/architecture-lint --quiet`.

## Child DOX Index

| Path | Contract |
|------|----------|
| `engine.py` | Synthesis orchestration and generated-record construction. |
| `validation.py` | Generated validation phases: static source/contract gates, sandbox fixtures, proof evidence, and deterministic admission. |
| `sandbox_runner.py` | Disposable sandbox preparation, mediated fixture execution, and bounded retry mechanics. |
| `proof_ledger.py` | Durable proof and verification evidence. |
| `promotion.py` | Promotion and lifecycle transitions. |
| `executor.py` | Validated generated-capability invocation, failure classification, and proof/candidate persistence projection; it owns no validation or policy authority. |
| `runtime.py` | Validated generated capability execution. |
| `child_runtime.py` | Sandbox child-process lifecycle, framed IPC, and bounded retries. |

- `validation_phases.py` owns explicit static, dependency, sandbox, and evidence phases; `Validator` coordinates those phases and does not become a second admission authority.
