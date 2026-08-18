# Athena

A compact, local-first autonomous agent runtime with durable knowledge,
structured delegation, universal execution, open capability protocols, and a
**single authoritative reasoning loop**.

Athena unifies the best ideas from Hermes Agent and Open Interpreter Classic
into one kernel. See `SPEC.md`, `BUILDSPEC.md`, `BEHAVIORSPEC.md`, and
`RESEARCHSPEC.md` for the authoritative contracts.

## Install (editable)

```bash
pip install -e ".[dev,cli]"
```

## Quickstart

```bash
athena --help
athena run "list the files in this directory using shell"
```

## Architecture (one line each)

- `athena.kernel` — the one reasoning loop (INV-001).
- `athena.capabilities` — `Registry -> Policy -> Executor`, the one capability path (INV-004).
- `athena.execution` — the one execution authority (INV-005).
- `athena.state` — the one session authority (INV-003).
- `athena.tasks` — the one task abstraction (INV-002).
- `athena.policy` — unbypassable policy + autonomy profiles (INV-008).
- `athena.{context,memory,skills,models,scheduler,mcp,acp}` — supporting subsystems.
- `athena.service` — composes the runtime; `athena.{cli,api}` — interfaces (INV-007).
