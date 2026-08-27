# Athena Architecture

**Status:** Target architecture and implementation alignment guide  
**Scope:** AgentKernel, Affordance Fabric, governance, programmable computer, and durable reality

Athena should feel as operationally useful as a broad computer-use agent, but
its internal model is intentionally different:

> **One durable intelligence inhabits a programmable computer and operates
> through a dynamically extensible, policy-governed affordance fabric.**

This document makes that design explicit. It is not permission to claim a
feature merely because its type or module exists. The implementation status at
the end of this document is the current boundary between design and reality.

## 1. Architectural identity

Athena preserves the useful external behavior associated with Hermes-like
agents:

```text
objective
  -> inspect
  -> execute
  -> write
  -> debug
  -> test
  -> create missing machinery
  -> retry
  -> verify
  -> return a durable result
```

It combines that reach with the Open Interpreter Classic idea that execution
is a programmable computer, not only a fixed list of tools. When the existing
surface is insufficient, Athena may construct a helper, adapter, analyzer,
capability, or workflow.

Athena must not become a collection of quasi-independent agents:

```text
AgentKernel                 decides
Capability / Workflow        acts according to a request
Skill / Memory               supplies knowledge
Provider                     infers
PolicyEngine                 authorizes
ExecutionManager             executes
State stores                 record
```

Only `AgentKernel` decides what to do next. A workflow is deterministic
composition, not another reasoning authority. A delegated child is an
ordinary bounded Task, not a different agent architecture.

## 2. Five cooperating layers

```text
                         AgentKernel
                    one reasoning authority
                              |
                              v
                       Affordance Fabric
          capabilities | workflows | skills | project knowledge
                              |
                              v
                         Governance
            policy | approvals | budgets | scopes | provenance
                              |
                              v
                    Programmable Computer Body
       execute | runtimes | PTY | process | filesystem | network | device
                              |
                              v
                         Durable Reality
       Tasks | events | messages | artifacts | mutations | evidence
```

The separation is semantic, not merely organizational:

```text
reasoning authority    != authorization authority
authorization authority != execution authority
execution authority    != persistence authority
```

Every interface (CLI, API, ACP, scheduler, delegation, or MCP) converges on
the same Task, capability, execution, event, and evidence primitives.

## 3. The Affordance Fabric

The fabric is the effective surface through which a Task discovers and uses
what its environment can do. It is broader than the native capability
registry and narrower than arbitrary host authority.

```text
Global CapabilityRegistry
          +
Project CapabilityOverlay
          +
Task CapabilityOverlay
          +
User capability/workflow overlays
          =
EffectiveAffordanceSurface(task)
```

The effective surface may contain:

| Kind | Meaning |
| --- | --- |
| Native capability | Athena-provided governed primitive. |
| Generated capability | Validated executable machinery with inherited authority. |
| External capability | MCP, plugin, device, or other adapter normalized into the same path. |
| Workflow | Declarative composition of capabilities and nested workflows. |
| Skill | Procedural knowledge that helps the kernel choose and use affordances. |
| Scratch program | Cheap task-local computation that is not automatically retained. |

The registry is an ergonomic and governed inventory. It is not the boundary
of Athena's agency: universal execution remains the escape hatch for building
new deterministic machinery, subject to the same authority wall.

### Reflection

Before choosing a strategy, the kernel should be able to ask the fabric for:

```text
capabilities.search(query)
capabilities.describe(id)
capabilities.dependencies(id)
capabilities.provenance(id)
capabilities.history(id)
capabilities.created_this_task(task_id)
```

The live reflection capability now also searches/describes visible workflows
and skills, and the ContextCompiler uses the fabric's deterministic ranked
search for progressive capability disclosure while retaining foundational
creation/reflection routes. Runtime, dependency, permission, device, and
project-affordance reflection remain future extensions of the same interface.

### OI Classic-aligned machinery patterns

The useful OI Classic contribution to this layer is a set of computational
patterns, not a second agent runtime:

| OI pattern | Athena form |
| --- | --- |
| Introspected `Computer` API | Capability/workflow reflection with typed descriptors and scoped visibility. |
| Persistent language sessions | ExecutionManager-owned runtime sessions, keyed and audited by Task. |
| Partial JSON/delta assembly | Provider-owned `ToolCallCandidate` plus one canonical response accumulator. |
| Large-output truncation and follow-up queries | Immutable artifacts with bounded previews and explicit retrieval/slice/summary operations. |
| Filesystem-backed skills | Validated Skill/Workflow candidates with provenance and explicit promotion. |
| Optional code scanning | Independent generated-source checks in the validation tier, never a substitute for sandbox authority. |

This gives Athena the OI behavior of building and reusing intermediate
machinery while preserving Athena's boundaries: generated code cannot import a
mutable host API, a skill cannot silently execute, and research helpers cannot
become an ungoverned network path. A generated input contract may be supplied
by the model or inferred from validation fixtures; when an output contract is
omitted, successful fixture observations can produce a concrete output schema
as well. Both are compiled as real JSON Schema before admission. Tool repair
always targets that resolved schema; it never derives a permissive schema from
malformed arguments.

These are five cross-cutting OI augmentations that remain part of Athena's
core operating model rather than optional polish:

| Augmentation | Athena contract | Current status |
| --- | --- | --- |
| Persistent computational sessions | Runtime state is keyed by Task/runtime session, serialized through the ExecutionManager path, and audited with execution events. | **Live in-process**; startup explicitly marks prior sessions dead and emits `RuntimeStateLost`; process reattachment and full backend conformance remain incomplete. |
| Reflection and progressive disclosure | The fabric can search/describe visible capabilities, workflows, and skills; ContextCompiler selects ranked relevant affordances while retaining foundational creation/reflection routes. | **Live for the current surface**; runtime/device/permission/dependency discovery remains partial. |
| One canonical response accumulator | Provider deltas and terminal responses assemble into one mixed `ModelResponse`, preserving text, reasoning, and parallel tool calls exactly once. | **Live** in kernel and registry collection paths. |
| Adaptive output artifacts | Large/structured execution output is retained as immutable, task-owned artifacts with bounded previews and explicit list/read/slice/search follow-up operations. | **Live for local artifacts**; richer MIME-aware extraction and fully nonblocking large-file I/O remain incomplete. |
| Independent generated-source checks | Generated machinery passes tiered parse/interface/security checks plus Ruff and, for durable scopes, Mypy before sandbox trials and registration. | **Live as an admission gate**; property-based tests, evidence scoring, and stronger external analyzers remain future work. |

The last two rows deliberately connect to, but do not collapse into, tool
repair. Tool repair is a bounded compatibility operation on one model-produced
argument candidate: parse/normalize, revalidate against the exact capability
schema, and either produce a canonical call or reject it. Generated-source
validation is an independent admission operation on executable machinery:
inspect source, validate its interface and contracts, run static checks, then
trial it under the restricted execution boundary. They share schema hashes,
provenance, and deterministic evidence formats; they must not share a
permissive parser or treat a repaired call as proof that generated code is
safe.

## 4. The operating loop

Workflows and generated machinery participate throughout the loop, not only as
an optional post-task preservation step.

```text
REASON
  |
  v
define next objective
  |
  v
discover affordances
  |
  v
choose strategy
  |-----------------------|----------------------|
  v                       v                      v
invoke capability      compose workflow      construct machinery
                                                  |
                                           synthesis workflow
                                                  |
                                      resolve dependencies
                                      write / compose
                                      validate
                                      trial execute
                                      register task-local
  |-----------------------|----------------------|
                          v
                   execute / operate
                          |
                          v
                   observe reality
                          |
             observation sufficient?
                    |             |
                   no            yes
                    |             v
          build observer/helper   update world state
                    |             |
                    +-------> verify
                                  |
                         |--------+--------|
                         v                 v
                       adapt            complete
                         |                 |
                         +--> REASON      v
                                  retain / promote
```

Athena can construct machinery during planning, execution, observation,
verification, debugging, or recovery. It chooses between direct computation,
composition, and knowledge preservation based on the missing affordance:

| Insufficiency | Preferred response |
| --- | --- |
| Missing primitive | Generate a capability or use a controlled dependency route. |
| Missing composition | Synthesize or compose a workflow. |
| Missing deterministic transformation | Create a scratch helper or generated analyzer. |
| Missing procedural knowledge | Create a skill candidate. |
| Missing adapter/observation format | Generate an adapter or normalizer. |
| Missing verification | Construct a deterministic verification workflow/helper. |

The kernel remains responsible for interpreting the result and deciding the
next step. Generated machinery does not gain a hidden decision loop.

## 5. Workflows

Workflows are durable, inspectable, deterministic compositions. They do not
replace Tasks and they do not contain an independent model loop.

### Workflow levels

1. **Task workflow** — the transient strategy for the current objective.
2. **Composition workflow** — a reusable sequence of existing capabilities.
3. **Synthesis workflow** — the procedure that constructs and admits new
   machinery.
4. **Validation workflow** — checks appropriate to the artifact being built.
5. **Observation workflow** — normalizes raw execution into structured evidence.
6. **Verification workflow** — checks acceptance criteria and binds evidence.

Workflows may be nested:

```text
release_candidate
  -> workflow: prepare
       -> format, lint, typecheck
  -> workflow: test
       -> unit, integration, e2e
  -> capability: build
  -> workflow: verify_release
```

Nested execution is deterministic and bounded. A workflow step references a
capability or another workflow by ID, resolves inputs declaratively, and emits
the same canonical capability, execution, and evidence events as direct use.
The AgentKernel decides why to run it and how to react to unexpected output.

## 6. Evidence and Research Fabric

Archivist contributes a complementary adaptive affordance: when Athena lacks
knowledge or trustworthy support, it can acquire and structure evidence under
the same Task, policy, artifact, and event boundaries. This is not an embedded
research agent. The AgentKernel remains the only reasoning authority; research
is implemented as ordinary capabilities and declarative workflows.

The durable knowledge types stay distinct:

| Object | Meaning |
| --- | --- |
| Memory | Something useful Athena or the operator previously concluded. |
| Skill | Procedural knowledge about how to accomplish something. |
| Evidence | Why a factual claim is believed, with source support. |
| Capability | A governed callable operation. |
| Workflow | A deterministic composition/procedure. |

The Evidence/Research Fabric records three primary objects:

```text
SourceRecord
  canonical URI + source version/content hash
  authority classification + retrieval/publication metadata
  immutable ArtifactStore snapshot reference

EvidenceObject
  source ID + extracted claim
  exact supporting excerpt + locator
  extraction provenance + confidence
  optional Athena claim ID
  corroboration/contradiction links

ResearchGap
  objective + unanswered question
  gap kind + required/open state
  evidence that closed the gap
```

The intended research workflow is:

```text
objective
  -> evidence requirements
  -> policy-controlled acquisition
  -> source snapshot/artifact
  -> extraction into EvidenceObjects
  -> corroboration and contradiction checks
  -> gap analysis
  -> targeted follow-up workflow
  -> claim/evidence binding and verification
```

Source authority is a ranking/classification signal, never authorization. A
source URI must pass an explicit pre-acquisition `SourcePolicy`; an empty
external-domain allowlist denies HTTP(S) sources by default. Captured local
artifact snapshots can be verified byte-for-byte against their supporting
excerpt. The current live route supports policy-controlled network
acquisition, artifact-backed snapshots, bounded lexical search/indexing,
durable source/evidence/gap records, excerpt verification, and deterministic
`research:plan`, `research:assess`, and `research:bundle` operations. Those
operations sequence explicit requirements and only close gaps backed by
verified captured evidence. Open-ended retrieval, semantic ranking, and
autonomous acquisition/critique remain future work.

Archivist's in-memory planner/critic loop is intentionally not imported. A
future `research.deep` or `research.verify_claim` workflow may extend the
current bounded plan/assess/bundle surface with search, fetch, extraction,
indexing, gap closure, and verification capabilities, but it cannot own its
own replanning brain or bypass Athena policy.

Research survives compaction in the same way generated machinery does: source
snapshots, hashes, evidence, contradictions, gaps, and provenance remain
available without replaying the original conversation. Research can also
inform Machinist-style construction—for example, official documentation can
define an adapter contract—while generated parsers can produce structured
evidence for later research.

## 7. Scratch and generated machinery

Scratch is deliberately cheap and task-local:

```text
ScratchProgram
  -> execute through the normal restricted execution route
  -> produce a structured observation
  -> retain only if useful
```

Not every short-lived helper deserves a durable capability. A useful scratch
program may be distilled into a `GeneratedCapability`, `Workflow`, `Skill`,
or project affordance only after evidence shows that retention is worthwhile.

A generated capability is an interface plus implementation metadata:

```text
GeneratedCapability
  id, name, description
  input_schema, output_schema
  implementation, runtime
  required_capabilities, required_dependencies
  declared_effects
  effective_authority       # calculated outside generated code
  scope and owner
  provenance
  code_hash, schema_hash
  validation_state
  proof_record
  version
```

The declared envelope is a request, not authority. Effective authority is
calculated externally, then enforced by the capability/policy/execution
boundary. Generated code must never be allowed to manufacture or widen its
own effects.

The live admission route is the model-visible `synthesis` capability. Its
`create` operation requires a Task, validates the supplied source and input
contract in the restricted synthesis runner, derives an output contract from
successful fixtures when needed, and installs the result only in
that Task's overlay. It returns the capability ID and proof record so the
kernel can immediately invoke the new tool. Promotion is a separate explicit
operation; a generated tool is never silently added to the global registry.
The current runner is deliberately conservative: generated code receives a
fixed restricted authority envelope, so a declaration such as `WRITE_LOCAL`
does not widen what it can do.

## 8. Dependency acquisition

Dependency installation is a governed operation, not an excuse to shell out
blindly:

```text
DependencyRequirement
        |
        v
dependency.resolve
        |
   already present?
      |       |
     yes      no
      |       v
      |    policy / approval
      |       |
      |    controlled install
      +-------+
              |
              v
          validation
```

Dependency records must include manager, name, version constraint, purpose,
owner/provenance, and the resulting environment fingerprint. Package managers
and network access are policy-controlled effects.

## 9. Validation tiers

Validation must match the artifact's intended lifetime:

| Scope | Minimum validation |
| --- | --- |
| Scratch | Parse and bounded smoke run. |
| Task | Parse, interface/schema contract, restricted trial execution. |
| Candidate | Task validation plus recorded evidence and effect review. |
| Project | Formatting, lint, type checks where applicable, fixtures/tests, schema contract, security review. |
| User | Project-level validation plus explicit promotion/approval and reproducible dependencies. |
| System | Normal Athena release process; never autonomous promotion. |

Validation is an independent workflow. A model reviewing its own generated
code is not sufficient evidence. The validator must record the code hash,
schema hash, validation policy/version, fixtures, environment, effective
authority, and observations.

## 10. Scope, retention, and promotion

```text
SCRATCH -> TASK -> CANDIDATE -> PROJECT -> USER -> SYSTEM
```

The scopes mean:

| Scope | Lifetime and visibility |
| --- | --- |
| Scratch | Current computation; no automatic registration. |
| Task | Visible only to one Task and removed at terminal finalization. |
| Candidate | Retained as a reviewable proposal with evidence. |
| Project | Available to the project overlay after explicit promotion. |
| User | Available across the user's projects after explicit promotion. |
| System | Shipped Athena functionality; requires normal release controls. |

Retention is evidence-based. Store creator Task, source event sequence,
validation evidence, code/schema/dependency hashes, environment constraints,
successful executions, failures, supersession, and usage. The current store and
fabric provide scoped deduplication, lifecycle history, durable proof updates,
quality/usage counters, explicit deprecation, and garbage collection. Review
UX, richer supersession workflows, and system-level release controls remain
future work.

Compaction must not erase a useful learned affordance. A future context turn
should be able to retrieve the promoted capability, skill, workflow, and its
provenance without replaying the original conversation.

## 11. Security and authority boundary

The central security invariant is:

> **Generated machinery expands behavior, never authority.**

The required path is:

```text
generated request
  -> external effect calculation
  -> policy / approval
  -> restricted capability and execution backend
  -> observation and audit
```

The following are not acceptable substitutes:

```text
generated code declares READ_LOCAL
  -> Athena trusts the declaration

copytree('/tmp/shadow')
  -> Athena assumes execution is isolated

direct subprocess.run(...)
  -> Athena calls it verification
```

Generated, scratch, workflow, shadow, dependency, and verification execution
must all use the canonical policy and execution boundaries. The backend must
provide the actual filesystem, network, process, secret, resource, and
workspace restrictions that the policy claims.

## 12. Alignment boundary

The following table is intentionally conservative. “Partial” means the
concept or a narrow path exists, not that the end-to-end product behavior is
complete.

| Design area | Current alignment |
| --- | --- |
| One AgentKernel, Task, policy, execution, and durable event model | **Mostly aligned**; existing core contracts support this model. |
| Affordance Fabric and task/project/user overlays | **Partial**; fabric, live task-scoped synthesis, durable project/user/candidate records, ownership filtering, lifecycle history, and proof updates exist; review and promotion UX remain incomplete. |
| Scratch lifecycle | **Partial**; records exist, but the kernel does not yet choose scratch/composition/synthesis through one explicit strategy surface. |
| GeneratedCapability | **Partial**; model-visible task-scoped creation, hashes, dependency locks, proof evolution, candidate retention, project/user rehydration, and deprecation exist; promotion UX and fully enforced sandbox semantics remain incomplete. |
| Declarative nested workflows | **Partial**; models, SQLite storage, validation, execution, and a capability route exist; kernel-level strategy integration and full conformance are incomplete. |
| Reflection | **Partial**; scoped/ranked capability reflection plus workflow/skill search and description are live; broader resource/runtime/device/permission discovery is incomplete. |
| Evidence/Research Fabric | **Partial**; durable source/evidence/gap records, artifact-backed excerpt verification, claim links, pre-acquisition source policy, bounded lexical indexing, and deterministic plan/assess/bundle operations are live; semantic retrieval, autonomous acquisition/critique, and full completion verification remain incomplete. |
| Dependency acquisition | **Partial**; a governed Python route records resolved versions, source metadata, file hashes, and environment fingerprints; manager breadth, lock replay, and full policy coverage remain. |
| Tiered validation | **Partial**; task admission now records parse/interface/security/format/lint checks, candidate/project/user tiers can require Ruff/Mypy, and exact JSON Schema is compiled; generated-test planning, independent evidence, and optional Semgrep remain incomplete. |
| Promotion and retention | **Partial**; explicit project/user promotion paths, durable generated proof, candidate retention, lifecycle history, quality scoring, deprecation, and garbage collection exist; richer review/supersession UX remains. |
| Authority inheritance and isolation | **Not release-ready** until every generated/shadow/verification path is backed by a real restricted backend. |

This table is an alignment guard. It prevents class names, comments, or
documentation from being treated as evidence that a subsystem is complete.

## 13. Design decision

Athena is not “Hermes Lite with Open Interpreter added.” The target is:

```text
Hermes-like operational reach
  + OI Classic programmable-computer behavior
  + one durable reasoning authority
  + capability-independent governance
  + durable learned machinery
```

The success condition is that Athena can use existing abilities, compose them,
construct missing deterministic machinery, validate and execute it under
inherited authority, learn reusable project affordances, and preserve those
affordances durably—without creating a second brain or a second execution
path.
