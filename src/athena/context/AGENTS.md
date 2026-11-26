# Context

## Purpose

Context compiles bounded, provenance-labeled input for inference and durable
digests.

## Ownership

Context owns gathering, ordering, bounding, compression coordination, and
context output. It does not reason, execute, or apply provider-specific policy.

## Local Contracts

- Requirements and evidence predicates must remain neutral and not import kernel authority.
- Provenance and boundedness are preserved through retrieval and compression.
- Context compilation must not silently expand capability or model authority.
- Promoted workflows are bounded, provenance-labeled suggestions; matching workflow descriptions and input schemas may enter ordinary context, but execution still validates required inputs and routes through the workflow capability.
- Adaptive recovery projections are bounded advisory task context, never authorization or execution.
- Strategy selection records are evidence-only: they carry the advisory route, baseline, budget, rollback, and acceptance context without granting action or promotion authority.

## Work Guidance

Extract requirements, retrieval, and digest mechanisms without creating a
second compiler or decision loop.

## Verification

Run context unit tests and kernel seam tests when compiled requirements change.

## Child DOX Index

| Path | Purpose |
|------|---------|
| `requirements.py` | Convert bounded context facts into declarative model requirements |
| `digest_builder.py` | Build bounded hierarchical digest values from compiled context |
| `digest.py` | Compatibility exports for the neutral protocol digest contract |
| `admission.py` | Incremental bounded-context admission, compression, and final accounting |
