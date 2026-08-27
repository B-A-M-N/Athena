# Athena

A compact, local-first autonomous agent runtime with durable knowledge,
structured delegation, universal execution, a programmable computer body, and
a **single authoritative reasoning loop**.

Athena unifies the best ideas from Hermes Agent and Open Interpreter Classic
into one kernel. See `SPEC.md`, `BUILDSPEC.md`, `BEHAVIORSPEC.md`, and
`RESEARCHSPEC.md` for the authoritative contracts, and
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the single-kernel,
programmable Affordance Fabric design and its current alignment boundary.

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
- `athena.affordances` — the effective surface of native, generated, external,
  task-local, and project-scoped abilities.
- `athena.capabilities` — `Registry -> Policy -> Executor`, the one capability path (INV-004).
- `athena.workflows` — durable declarative composition; workflows do not create
  another reasoning loop.
- `athena.research` — durable source snapshots, evidence objects, claim links,
  and research gaps; acquisition remains explicitly policy-controlled.
- `athena.execution` — the one execution authority (INV-005).
- `athena.state` — the one session authority (INV-003).
- `athena.tasks` — the one task abstraction (INV-002).
- `athena.policy` — unbypassable policy + autonomy profiles (INV-008).
- `athena.{context,memory,skills,models,scheduler,mcp,acp}` — supporting subsystems.
- `athena.service` — composes the runtime; `athena.{cli,api}` — interfaces (INV-007).

## Operating model

Athena is intended to feel as operationally useful as a broad computer-use
agent while remaining one durable intelligence internally:

```text
                         AgentKernel
                              │
                              ▼
                       Affordance Fabric
          capabilities │ workflows │ skills │ evidence
                              │
                              ▼
                 policy, approvals, budgets, scopes
                              │
                              ▼
                    Programmable computer body
       execute │ runtimes │ PTY │ files │ processes │ network │ devices
                              │
                              ▼
                         Durable reality
       tasks │ events │ artifacts │ mutations │ claims │ provenance
```

The Fabric is deliberately broader than a static tool registry. It combines
native, generated, external, task-local, project, and user-scoped affordances.
The kernel can use an existing capability, compose a workflow, acquire missing
knowledge, or construct deterministic machinery when the current surface is
insufficient. Workflows and capabilities act; only the `AgentKernel` decides
what to do next.

This is the intended synthesis of three complementary ideas:

- **Open Interpreter Classic:** the computer is programmable, persistent, and
  introspectable; code is an escape hatch for building missing intermediate
  machinery.
- **Machinist:** durable machinery follows a manufacturing pipeline of
  specification, implementation, independent checks, contract tests, sandbox
  trials, provenance, and explicit promotion.
- **Archivist:** missing knowledge follows an evidence pipeline of
  policy-controlled acquisition, source snapshots, retrieval, claims,
  citations, contradiction/gap analysis, and durable retention.

Neither Machinist nor Archivist is a second agent inside Athena. Their useful
ideas are expressed as ordinary capabilities, workflows, artifacts, evidence,
and events under the same policy and execution boundaries.

## Adaptive affordances

Athena has several intentionally distinct ways to extend its operating
surface:

```text
Scratch computation
    cheap, task-local helper; no automatic retention

Generated capability
    validated callable machinery with a schema and provenance

Workflow
    deterministic composition of capabilities and nested workflows

Skill
    procedural knowledge that helps the kernel choose an approach

Evidence / research
    durable support for factual claims and decisions
```

The generated-machinery lifecycle is scoped and explicit:

```text
SCRATCH → TASK → CANDIDATE → PROJECT → USER → SYSTEM
```

Task-local machinery is visible only to its owning Task and is removed at
terminal cleanup. Project and user promotion are separate operations. System
promotion is a normal release concern, never autonomous self-modification.

Generated code expands behavior, not authority. A model-declared effect list
is audit metadata, not permission. Effective authority is calculated outside
the generated code and enforced by the canonical capability/policy/execution
path. Source validation, JSON Schema contract checks, bounded tests, and
restricted execution are independent admission requirements; a repaired tool
call is not evidence that the implementation is safe.

### Generated-code quality and dynamic contracts

Generated machinery is admitted through a deterministic quality gate before it
becomes callable:

```text
source
  -> parse/interface checks
  -> security-pattern checks
  -> canonical formatting (Ruff when available)
  -> lint (Ruff)
  -> typecheck for durable scopes (Mypy)
  -> exact JSON Schema compilation
  -> bounded fixture/smoke execution in the restricted backend
  -> task/project overlay registration
```

The input contract may be supplied explicitly or generated from positive
validation fixtures. If an output contract is omitted, successful fixture
observations produce a concrete output schema. Both contracts are compiled
and revalidated at the same boundary used for ordinary tool calls, so a
generated helper cannot quietly fall back to an unconstrained `{}` contract.

Tool repair is the adjacent, narrower gate for model-produced calls. It keeps
raw malformed arguments, applies one deterministic schema-directed repair
pass, records a receipt, and strictly revalidates the result. It may correct a
bad call shape; it never formats, tests, or certifies the generated
implementation behind that call. Those concerns remain separate and both
must pass before generated machinery is retained or promoted.

The repository exposes the same discipline as repeatable developer gates:
`make format` applies Ruff formatting, `make lint` checks correctness-focused
Ruff rules, `make typecheck` runs Mypy, `make compile` catches import/bytecode
syntax failures, and `make check` runs those static gates plus the highest-risk
generated-machinery and model/tool contract tests. The broader formatter check
is available as `make format-check`; existing legacy formatting is being
normalized incrementally rather than hidden behind a false clean claim.

## 90-second demo

The offline Capability Fabric showcase is reproducible with VHS.  The
authoritative path for the produced GIF is `demos/capability_fabric.gif`,
and the canonical event stream the demo renders is
`demos/fixtures/capability_fabric.jsonl` (emitted by the demo driver and
replayed by the recording).

Always render through the single-writer wrapper so the artifact lifecycle
is race-free and process-exit is verified before the gif is treated as
complete:

```bash
scripts/render-demo              # default demo: capability_fabric
scripts/render-demo capability_fabric
```

The wrapper acquires an exclusive lock, renders to a unique temp path,
verifies VHS exit 0 and that the resulting GIF decodes (GIF89a magic,
1280x720, frame count, bounded duration), and atomically renames the
temp into place.  A bounded timeout (~60s) kills the whole VHS process
group on overrun, leaving any previous known-good gif untouched.

Direct `vhs demos/capability_fabric.tape` still works for ad-hoc local
capture but does not provide the lock, timeout, or decode validation
the stable-beta gate requires.

It demonstrates the intended operating loop—discover a missing affordance,
acquire bounded evidence, construct a deterministic helper, infer its contracts,
run independent source checks, repair one malformed model call, execute under
inherited authority, and retain the useful result. It uses real Athena
protocol/validation primitives but no provider, network, or host mutation.

Research uses the same durable Task and evidence model. A source is fetched
only after passing source/network policy, retained as an artifact-backed
snapshot, and linked to claims through locators and supporting excerpts. A
future research workflow may search, retrieve, index, challenge, and fill
evidence gaps, but it does not create a separate research brain.

## What Athena deliberately does not mean

Athena is not a giant predefined-tool agent, a collection of independently
reasoning subagents, or a claim that every present backend is production-ready.
The architecture document records the current alignment boundary. In
particular, full host isolation, durable causal replay, deep research/indexing,
and some specialized runtime/UI backends remain active implementation work.
Types, registries, and documentation are not by themselves evidence that a
subsystem is complete.

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
| `/undo MUTATION` | Roll back one completed mutation via `RollbackExecutor`. |
| `/compact` | Context-window size, output reserve, and recent-verbatim-turn settings. |
| `/criteria LIST` | Set acceptance criteria for the next task (`;`-separated; `command:` prefix = probe). |
| `/interrupted`, `/resume [ID]` | List/re-queue tasks parked by shutdown or crash. |
| `/context` | What the next model turn will see (durable messages + inclusion rules). |
| `/details` | Toggle raw model deltas and per-event diagnostics. |
| `/cancel`, `/sessions`, `/new`, `/autonomy`, `/model` | Task/session control. |

> **Note:** `CheckpointManager` snapshots are **workspace-file snapshots**
> (files + manifest). They do not yet capture runtime variables, processes,
> database state, or environment — that fuller "computational checkpoint" is
> future work. The `workspace.snapshot/restore` capability carries the same
> scope.

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

### Role-divided models

Different roles can use different models. Roles without an assignment fall
back to whatever the user configured globally (the "primary" choice):

```toml
# athena.toml (or ATHENA_MODEL_ROLES='{"summarizer":{"allowed":["prov/cheap"]}}')
[model_roles.summarizer]
allowed = ["openai/gpt-4o-mini"]     # cheap model for context compression
max_cost_usd = 0.01

[model_roles.judge]
allowed = ["anthropic/claude-sonnet-4"]   # acceptance-criteria judging

[model_roles.primary]
allowed = ["anthropic/claude-opus-4"]     # main reasoning loop
```

Built-in roles: `primary` (task reasoning), `summarizer` (context compression),
`judge` (model-judged acceptance criteria). A caller's explicit allowlist
always wins over role defaults.

### Post-task knowledge pipeline

Every completed or partial task feeds Athena's durable knowledge: an episodic
record of the task outcome is saved immediately, conservative lesson
candidates are stored as `pending_promotion` (never auto-trusted), and skill
drafts are validated and recorded for explicit promotion later (BHV-099/102/107).

### Acceptance criteria

```bash
athena run "fix the failing test" --criteria "command:pytest -q;no TODO left in src"
```

`command:` criteria run as executable probes; the rest are judged by the
`judge`-role model against task evidence. Claimed completion with unverified
criteria resolves to PARTIAL with the unresolved items recorded — never a
false COMPLETE.

### Resume after crash/shutdown

```text
/interrupted          # list tasks parked by shutdown or crash
/resume [task_id]     # re-queue the original durable task (same objective,
                      # criteria, workspace, policy) — not a new conversation
```

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
