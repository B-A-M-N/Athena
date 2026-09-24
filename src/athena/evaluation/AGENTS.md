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
