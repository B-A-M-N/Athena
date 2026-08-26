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

## Operator surface

`athena chat` and `athena run` use a calm, OI-inspired operator surface over
Athena's durable task events. Generated Python/shell code, runtime output,
artifacts, and failures remain visible, but noisy deltas are grouped into
readable execution cards. Supervised actions open a selectable approval menu
for call/task/session/project scope; the UI submits that decision through
`AthenaService`, so it never becomes a second agent loop or execution path.

### Stable views (REPL meta-commands)

| Command | What it shows |
|---|---|
| `/permissions` | Active policy grants (scope, capability, expiry) and pending approvals. |
| `/diff [N]` | The last N file mutations from the write-ahead mutation ledger. |
| `/undo MUTATION_ID` | Roll back one completed, reversible mutation via `RollbackExecutor`. |
| `/compact` | Context-window size, output reserve, and recent-verbatim-turn settings. |
| `/context` | What the next model turn will see (durable messages + inclusion rules). |
| `/details` | Toggle raw model deltas and per-event diagnostics. |
| `/cancel`, `/sessions`, `/new`, `/autonomy`, `/model` | Task/session control. |

Every view is a projection of the same canonical stores the kernel reads
(`ApprovalStore`, `MutationStore`, `MessageStore`, artifact store); none of
them mutate state except `/undo`, which goes through the service.

### Direct execution: `!` vs `!!`

Both escapes run shell commands through the canonical
`registry -> policy -> executor` path — policy, approval, recording, and
artifacts apply exactly as for model-requested execution. They differ only in
context handling:

- `!cmd` — result is recorded in the session transcript AND injected into the
  next model turn.
- `!!cmd` — result is recorded for audit but excluded from model context
  (`inject_into_context=False`; filtered at the context compiler boundary).

Neither escape routes through model inference. Both belong to the current
durable session and appear in `/sessions`, `/diff`, and the audit trail.

Example:

```
$ athena chat
> !!ls -la            # look around without polluting model context
┌─ execute ──────────────────────────────
│ language: shell
│ code: ls -la
└──────────────────────────────────────
  direct command · displayed only
  ✓ execute completed

> !cat notes.md       # this output IS available to the next turn
> fix the bug described in notes.md   # model sees the `!` output above
┌─ approval required ────────────────────
│ capability: execute
│ choose authorization scope:
│   1) call
│   2) task
│   3) session
│   d) deny
└──────────────────────────────────────
approval [1-3 / d] 2
```
