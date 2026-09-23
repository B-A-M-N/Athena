# Models

## Purpose

Models own provider adaptation, canonical inventory, admission, ranking, and
inference routing mechanics.

## Ownership

Model infrastructure describes and routes available models. It does not own
task reasoning, policy authorization, or application composition.

## Local Contracts

- Normalize provider discovery, configuration, and profiles into one effective model descriptor before admission.
- Admission is hard capability/policy filtering; ranking is preference/reliability/cost ordering.
- Router diagnostics must identify why candidates were rejected.
- Providers adapt wire protocols and do not choose global retry or policy behavior.

## Work Guidance

Keep provider-specific logic behind the provider boundary and avoid making the
router query several competing metadata authorities.

## Verification

Run model, provider compatibility, routing, and admission tests.

## Child DOX Index

| Path | Purpose |
|------|---------|
| `providers/` | Wire-protocol provider adapters |
| `inventory.py` | Effective provider/model metadata normalization |
| `admission.py` | Hard model policy, capability, capacity, and privacy gates |
| `ranking.py` | Preference and reliability ordering after admission |
| `response_collection.py` | Direct provider-stream collection, local output limits, cancellation, and incomplete-outcome classification |
| `request_bounds.py` | Neutral provider-wire request sizing before model admission and durable attempt creation |
