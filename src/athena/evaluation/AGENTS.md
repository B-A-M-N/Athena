# Evaluation

## Purpose

This package provides neutral comparison corpora, fixed cases, and deterministic quality measurement.

## Authority And Evidence Boundaries

- Harnesses measure only explicitly declared dimensions and adapters; they do not authorize task work.
- Model prose is never completion or safety proof. Those facts must arrive as adapter-supplied explicit fields or evidence.
- Reports preserve case identity and comparable metrics without inventing missing outcomes.

## Verification

- Run evaluation harness tests plus `./scripts/architecture-lint --quiet`.

### Skill Selection Evaluation

- `skill_selection.py` owns held-out paraphrase/adversarial fixtures and outcome comparison; it does not activate skills or alter selection policy.

### Competitive Comparison

- `competitive.py` owns the fixed Athena/Hermes corpus, independent read-only oracles, and outcome classification; it does not activate capabilities or certify release readiness.
- `scripts/competitive-benchmark` records smoke or repeated-candidate evidence only. `release_qualified` remains false until representative mutation, recovery, safety, and cost comparisons are present; a blocked provider leg is not a failure or a pass.
