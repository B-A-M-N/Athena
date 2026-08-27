# Athena

A compact, local-first autonomous agent runtime with durable knowledge,
structured delegation, universal execution, a programmable computer body, and
a **single authoritative reasoning loop**.

Athena brings capability discovery, evidence, execution, policy, and learning
into one durable kernel. The normative contracts live in `SPEC.md`,
`BUILDSPEC.md`, `BEHAVIORSPEC.md`, and `RESEARCHSPEC.md`; the architectural
overview is in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

> **Status: active development.** Athena is a working prototype with a broad
> test-backed core, not a production-ready agent. Interfaces and subsystem
> boundaries may change while the runtime is being built.

## Why this exists

Athena comes from a simple, long-held ambition: put an agent in an unfamiliar
environment and let it find its footing, construct the missing machinery, and
make useful progress without a manual for every situation.

The interesting part is not any one tool. It is watching a system recognize a
gap, reach for the right affordance, and build a bounded solution that can be
checked and reused. Athena is an attempt to make that behavior durable: one
reasoning loop, explicit authority, retained evidence, and an auditable path
from decision to execution.

This is still a working prototype, but the direction is deliberate: capability
should expand through evidence and disciplined construction, not through an
unbounded collection of loosely coordinated agents.

## Install

Requires Python 3.12 or newer.

```bash
pip install -e ".[dev,cli]"
```

## Development

Athena is easiest to work on from an editable install. The repository provides
small, repeatable gates for the main development loop:

```bash
make format          # apply Ruff formatting
make lint            # correctness-focused Ruff checks
make typecheck       # run Mypy
make compile         # catch import and bytecode errors
make check           # run the full local verification gate
```

`make format-check` verifies formatting without changing files. The broader
formatter baseline is being normalized incrementally while the project is in
active development.

## Quickstart

```bash
athena --help
athena chat
```

Inside the console, direct commands use the same policy and execution path as
model-requested commands:

```text
athena> !!pwd       # show and record output, but keep it out of model context
athena> !ls         # show, record, and provide output to the next model turn
```

The CLI includes a deterministic fake provider for offline smoke runs. It is
useful for wiring and UI checks, not general-purpose reasoning. OpenRouter is
the current hosted-provider path:

```bash
export OPENROUTER_API_KEY="<your-key>"
export OPENROUTER_MODEL="poolside/laguna-s-2.1:free"  # optional; free route
athena run "explain the files in this project" --autonomy autonomous
```

Athena keeps the key in its credential boundary and does not write it to the
config, task state, or repository. The default is configured as the free
`poolside/laguna-s-2.1:free` route; availability on shared free capacity can
vary. Other `:free` model IDs can be selected with `OPENROUTER_MODEL`.

## Architecture at a glance

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
- `athena.scheduler` — durable trigger + claim engine; fires due occurrences and
  enqueues Tasks without creating a second reasoning loop.
- `athena.shadow` — speculative execution in isolated branch clones of the ONE
  agent; nothing runs outside the canonical kernel/policy/executor path.
- `athena.causal` — causal task forks from any event point, plus checkpoint
  support so a fork can restore to an exact causal state rather than replay.
- `athena.worldstate` — durable claims, invariants, and execution-grounded
  task reality.
- `athena.fusion` — orchestrates shadow + worldstate + causal + synthesis as
  ONE system: speculative experiments, invariant-gated commits, claim
  invalidation on commit, proof-carrying synthesis.
- `athena.interpreter` — the single-routing authority that proposes/adapts
  machinery through the canonical path.
- `athena.recovery` — startup crash recovery: reconciles in-flight state after
  a hard crash.
- `athena.{context,memory,skills,models,mcp,acp}` — supporting subsystems.
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

The design carries forward three complementary lines of work:

- **Open Interpreter Classic:** a programmable, persistent, introspectable
  computer where code can fill a missing intermediate capability.
- **Machinist:** a disciplined construction pipeline of specification,
  implementation, independent checks, contract tests, provenance, and explicit
  promotion.
- **Archivist:** an evidence pipeline for policy-controlled acquisition, source
  snapshots, claims, citations, contradiction analysis, and retention.

These are expressed as ordinary capabilities, workflows, artifacts, evidence,
and events under the same policy and execution boundaries. They are design
influences and supporting concepts, not additional reasoning loops inside
Athena.

## Fused execution model

The kernel stays the single reasoning authority, but it composes a family of
durable, auditable mechanisms that all live inside the same event log and the
same kernel/policy/executor path:

```text
       ShadowEngine — speculative execution in isolated branch clones
       TaskWorldState — claims + evidence + invariants + machine reality
       TaskForker — causal forks from any event point (checkpoint-aware)
       CheckpointManager — workspace snapshots
       SynthesisEngine — ephemeral capabilities -> proof-carrying skills
                              │
                    Fusion orchestrator
                              │
                 proposals, invariant-gated commits,
                 claim invalidation, proof-carrying synthesis
                              │
                    AgentKernel (decides what happens next)
```

Typical integrated workflows:

- **Speculative experiments** — propose operations, execute in a shadow branch,
  verify, record a claim bound to the evidence, check invariants before commit,
  and commit only when the envelope holds. If the experiment fails, fork the
  parent task from the pre-experiment event for an alternate approach.
- **Invariant-gated commits** — no shadow branch commits while a required
  invariant is violated; violations are recorded as world-state facts.
- **Claim invalidation on commit** — committing a branch staleness-invalidates
  every claim whose dependency paths overlap the committed files.
- **Proof-carrying synthesis** — synthetic capabilities validated in shadow
  branches carry their branch id as provenance; repeated success converts them
  to skill candidates with that evidence attached.
- **Fork-with-checkpoint** — forking can first capture a parent-workspace
  checkpoint so the fork restores to the exact causal state rather than merely
  replaying events.

Durably scheduled work is the same idea applied to time. `athena.scheduler` is
a trigger + claim engine that fires due occurrences and enqueues Tasks; it
*creates Tasks only* and never runs an agent loop, so scheduling never becomes
a second reasoning brain.

Startup is reconciled through `athena.recovery`, which transitions in-flight
state left behind by a hard crash back to a consistent surface before the
kernel resumes.

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

## Demo (work in progress)

The offline Capability Fabric showcase is reproducible with VHS and uses the
same `DualPaneSurface` that powers `athena chat` and `athena run`. The
committed recording is a generated preview of the current operator surface.
It is expected to change while the UI and pacing are refined:

![Athena operator surface — Capability Fabric demo](demos/capability_fabric.gif)

The canonical fixture is `demos/fixtures/capability_fabric.jsonl`. It is
replayed through the real CLI surface. The intended task flow is to discover a
missing affordance, bind local evidence, synthesize and validate a task-local
helper, repair a malformed call, request approval, execute under inherited
authority, and retain the result.

Always render through the single-writer wrapper so the artifact lifecycle
is race-free and process-exit is verified before the gif is treated as
complete:

```bash
scripts/render-demo              # renders the current default recording
scripts/render-demo capability_fabric
```

The wrapper acquires an exclusive lock, renders to a unique temp path,
verifies VHS exit 0 and that the resulting GIF decodes (GIF89a magic,
1280x720, frame count, bounded duration), and atomically renames the temp
into place. A bounded timeout kills the whole VHS process group on overrun and
leaves any previous known-good GIF untouched. Rendering requires `vhs` and
`ffmpeg` on `PATH`.

Direct `vhs demos/capability_fabric.tape` still works for ad-hoc local
capture but does not provide the lock, timeout, or decode validation used by
the stable-beta gate.

For a quick local preview, pass a higher speed multiplier to the driver:

```bash
.venv/bin/python demos/capability_fabric_demo.py \
  --replay demos/fixtures/capability_fabric.jsonl --speed 8
```

The demo uses real Athena protocol, validation, event, and operator-surface
primitives but no model provider, network, database, or host mutation.

Research uses the same durable Task and evidence model. A source is fetched
only after passing source/network policy, retained as an artifact-backed
snapshot, and linked to claims through locators and supporting excerpts. A
future research workflow may search, retrieve, index, challenge, and fill
evidence gaps, but it does not create a separate research brain.

## Current limitations

Athena is not a giant predefined-tool agent, a collection of independently
reasoning subagents, or a claim that every present backend is production-ready.
The architecture document records the current alignment boundary. In
particular, full host isolation, durable causal replay, deep research/indexing,
and some specialized runtime/UI backends remain active implementation work.
Types, registries, and documentation are not by themselves evidence that a
subsystem is complete.

## Operator surface

`athena chat` and `athena run` use a calm operator surface over
Athena's durable task events. Generated Python/shell code, runtime output,
artifacts, and failures remain visible, but noisy deltas are grouped into
readable execution cards. Supervised actions open a selectable approval menu
for call/task/session/project scope; the UI submits that decision through
`AthenaService`, so it never becomes a second agent loop or execution path.

`athena inspect TASK_ID` is a deep task-observability command that surfaces
durable provider/model usage rows and task activity for a given task.
`athena oi-stream` opens the live OI window as a full-pane first-class view:
an unbuffered model/runtime stream with a mascot header and inline approvals.

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

---

## Infrastructure support and disclosure

> **Athena would not have reached its current state without the services of
> [FreeInference.org](https://freeinference.org/).**

This project was built and validated using model-inference access provided by
FreeInference.org, which makes free inference available to the public. That
access made it possible to develop and validate capable systems without the
hardware or budget that would otherwise be required.

For clarity and full disclosure:

- **FreeInference did not commission, direct, review, or pay for this work.**
  This is not a sponsored contribution, an endorsement by FreeInference, or a
  statement on their behalf. They are neither a partner nor a backer of Athena.
- This acknowledgment is offered freely and out of gratitude, not obligation.
  Open access to capable inference infrastructure is a meaningful enabler for
  open-source developers and researchers.

Free public inference is a valuable and vital societal resource worth
sustaining. If you or your organization can provide GPU capacity, hardware,
cloud credits, research funding, or other infrastructure support, please
consider supporting FreeInference so it can continue serving open-source
development, research, and education.
