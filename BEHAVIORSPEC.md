# Athena

## Behavioral Specification

**Document:** `BEHAVIORSPEC.md`
**Status:** Normative behavioral specification
**Project:** Athena
**Derived from:** `SPEC.md`, `RESEARCHSPEC.md`, `IMPLEMENTATIONSPEC.md`
**Implementation target:** Python 3.12+
**Primary platforms:** Linux, macOS, Windows
**Primary operating mode:** Local-first, headless-first, asynchronous
**Primary interface:** CLI
**Secondary interfaces:** HTTP/SSE, ACP
**Scope:** Externally observable runtime behavior, state transitions, authorization behavior, execution semantics, recovery behavior, and conformance criteria

---

# 1. Purpose

This document defines **how Athena MUST behave**.

`SPEC.md` defines product intent. `RESEARCHSPEC.md` validates and constrains that intent. `IMPLEMENTATIONSPEC.md` is the implementation authority and establishes the architecture engineers must build.

`BEHAVIORSPEC.md` translates those requirements into observable contracts suitable for:

* acceptance tests;
* integration tests;
* black-box tests;
* crash tests;
* security tests;
* provider/runtime contract tests;
* interface conformance tests;
* regression testing;
* clean-room behavioral implementation.

This document is **not** an alternate architecture specification.

If this document conflicts with `IMPLEMENTATIONSPEC.md`, `IMPLEMENTATIONSPEC.md` wins.

Behavior which is intentionally left undefined upstream is marked:

```text
IMPLEMENTATION-DEFINED
```

rather than silently invented.

---

# 2. Normative Language

The terms:

```text
MUST
MUST NOT
REQUIRED
SHOULD
SHOULD NOT
MAY
```

are normative.

A `MUST` or `MUST NOT` violation is release-blocking for the affected feature.

---

# 3. Behavioral Thesis

Athena behaves as:

> **One durable task runtime in which a single reasoning loop iteratively observes state, builds bounded context, invokes a policy-selected model, requests capabilities, observes their results, verifies completion, and terminates with a truthful result.**

The observable loop is:

```text
Task
  ↓
Context
  ↓
Model
  ↓
Decision
  ↓
Capability request?
 ┌───────┴────────┐
 yes              no
  │                │
authorize       evaluate
  │             completion
execute             │
  │                 │
observe         terminal?
  │              │     │
  └──── loop ────┘    yes
                       │
                    verify
                       │
                    result
```

There MUST NOT be a second externally observable reasoning architecture for delegation, scheduling, MCP, ACP, HTTP, computer control, or another interface.

The upstream architecture explicitly requires one authoritative reasoning loop and one universal task abstraction.

---

# 4. Behavioral Actors

Athena recognizes the following logical actors.

## 4.1 User

A human or authorized external caller initiating, redirecting, approving, denying, cancelling, or inspecting work.

## 4.2 Agent

The reasoning process represented by `AgentKernel`.

It decides what computational step to request next.

It does **not** grant itself permission.

## 4.3 Policy authority

Determines whether requested effects are:

```text
ALLOW
ASK
DENY
```

## 4.4 Capability executor

Performs an authorized operation.

## 4.5 Runtime

Executes code or commands and reports observable results.

## 4.6 Provider

Performs model inference.

## 4.7 Scheduler

Determines when configured work becomes due and creates a task.

It does not independently reason.

## 4.8 Child task

A normal Athena task created through delegation with explicitly bounded context, capabilities, credentials, model policy, and budget.

---

# 5. Global Behavioral Invariants

## BHV-001 — One decision authority

Only the Athena reasoning kernel MAY decide the next model-facing agent step.

A capability, runtime, scheduler, MCP server, plugin, interface, memory provider, or skill MUST NOT independently continue the agent reasoning loop.

---

## BHV-002 — All autonomous work becomes a task

Interactive requests, delegated work, scheduled work, HTTP requests, ACP requests, and future gateway-originated work MUST converge on the same task lifecycle.

No interface may expose materially different completion, policy, persistence, or cancellation semantics merely because of its transport.

---

## BHV-003 — Authorization precedes effects

A model-requested capability MUST NOT produce its requested external effect until policy evaluation has allowed it or a required approval has been granted.

The required capability lifecycle is:

```text
REQUESTED
   ↓
VALIDATED
   ↓
POLICY_EVALUATION
   ├── DENIED
   ├── WAITING_APPROVAL
   │       ├── DENIED
   │       └── APPROVED
   ↓
STARTED
   ├── FAILED
   ├── CANCELLED
   └── COMPLETED
```

Every significant transition SHOULD produce an event.

---

## BHV-004 — Execution cannot bypass policy

Shell, Python, PowerShell, Node, direct CLI shell escape, persistent REPL execution, and future execution backends MUST remain constrained by the effective task policy.

A filesystem restriction cannot be rendered meaningless by invoking shell commands instead.

---

## BHV-005 — Truth outranks apparent success

Athena MUST NOT report `complete` merely because the model generated language indicating completion.

A task with failed mandatory acceptance criteria MUST NOT be `complete`.

---

## BHV-006 — Unknown state remains unknown

After a crash or ambiguous side effect, Athena MUST NOT infer success solely to simplify recovery.

If execution outcome cannot be proven, the state MUST remain truthfully represented as interrupted, unknown, partial, failed, blocked, or recovery-required as appropriate.

---

## BHV-007 — No silent privilege inheritance

Child tasks MUST NOT automatically receive the parent's complete capabilities, credentials, network privileges, filesystem authority, computer authority, or model privileges.

---

## BHV-008 — No silent privacy or cost escalation

Athena MUST NOT silently transition:

```text
local model → remote model
private provider → less-private provider
free model → paid model
offline execution → networked execution
```

unless the active policy expressly permits that transition.

---

## BHV-009 — No universal undo guarantee

Athena MAY identify individual mutations as reversible.

Athena MUST NOT claim that arbitrary shell commands, network operations, messages, publishing operations, process effects, package installs, or computer actions are generically reversible.

---

## BHV-010 — Interface-neutral semantics

The same underlying task viewed through CLI, HTTP/SSE, or ACP MUST have the same canonical:

* task ID;
* session state;
* task status;
* capability history;
* approval history;
* mutations;
* event history;
* completion result.

Presentation MAY differ.

Semantics MUST NOT.

---

# 6. Request Intake

## BHV-011 — Request normalization

A valid agent request MUST ultimately produce or reference a `TaskSpec`.

The user-facing request MAY specify:

* prompt/objective;
* existing session;
* workspace;
* model policy;
* autonomy profile;
* attachments;
* requested capability limits;
* metadata.

After normalization, downstream reasoning MUST operate on canonical task state rather than interface-specific request objects.

---

## BHV-012 — Task creation

When a new task is accepted, Athena MUST:

1. assign an opaque task ID;
2. persist the task before autonomous work begins;
3. bind or create its session;
4. record its objective and applicable acceptance criteria;
5. resolve its workspace and policy;
6. initialize budget accounting;
7. emit task-creation state/events;
8. enqueue or directly make the task runnable according to execution mode.

---

## BHV-013 — Invalid requests

A request that cannot be normalized MUST fail before agent execution.

No provider call or capability side effect may occur for a request rejected during normalization.

The failure SHOULD use a structured error rather than free-form text alone.

---

# 7. Task Lifecycle

The canonical task state machine is:

```text
CREATED
   │
   ▼
QUEUED
   │
   ▼
RUNNING
   │
   ├──────────► WAITING_APPROVAL
   │                  │
   │                  └────► RUNNING
   │
   ├──────────► WAITING_INPUT
   │                  │
   │                  └────► RUNNING
   │
   ├──────────► BLOCKED
   │
   ├──────────► PARTIAL
   │
   ├──────────► FAILED
   │
   ├──────────► CANCELLED
   │
   ├──────────► INTERRUPTED
   │
   └──────────► COMPLETE
```

Illegal transitions MUST be rejected. `INTERRUPTED` is a recovery state and MAY later resume.

---

# 8. Task State Semantics

## BHV-014 — CREATED

`CREATED` means durable task identity exists but the task has not yet been made available for execution.

No autonomous reasoning is implied.

---

## BHV-015 — QUEUED

`QUEUED` means Athena considers the task eligible for worker acquisition but it is not currently executing.

---

## BHV-016 — RUNNING

`RUNNING` means the task may:

* compile context;
* invoke providers;
* dispatch authorized capabilities;
* spawn explicitly permitted child tasks;
* perform acceptance verification.

---

## BHV-017 — WAITING_APPROVAL

Athena MUST enter `WAITING_APPROVAL` when progress depends on an unresolved policy decision requiring human approval.

While in this state:

* the protected capability MUST NOT start;
* the task's durable state MUST show what approval is pending;
* cancellation MUST remain possible;
* approval or denial MUST be durably recorded.

On approval, the task MAY return to `RUNNING`.

On denial, the capability MUST NOT execute.

The task MAY subsequently continue through an alternate plan, become blocked, or terminate according to the agent's next decision.

---

## BHV-018 — WAITING_INPUT

Athena MAY enter `WAITING_INPUT` when essential user information cannot safely or meaningfully be inferred.

While waiting:

* the task MUST remain resumable;
* current task state MUST remain durable;
* no task completion may be claimed.

Providing the required input returns the task to `RUNNING`.

---

## BHV-019 — BLOCKED

`BLOCKED` means the objective cannot currently proceed because of an unresolved external requirement such as:

* denied required authority;
* unavailable required resource;
* unavailable credential;
* unavailable mandatory runtime/provider;
* unmet external dependency;
* required input that cannot currently be obtained.

The final result MUST identify the blocker.

---

## BHV-020 — PARTIAL

`PARTIAL` means meaningful requested work was completed but one or more material requirements or acceptance criteria remain unresolved.

The result MUST distinguish completed work from unresolved work.

---

## BHV-021 — FAILED

`FAILED` means the task terminated because execution or reasoning encountered a failure from which the task did not recover and useful completion semantics cannot justify `PARTIAL` or `BLOCKED`.

---

## BHV-022 — CANCELLED

`CANCELLED` means cancellation was requested and Athena has transitioned the task to its cancellation terminal state.

Owned execution resources and child work MUST be cleaned up according to the cancellation contract.

---

## BHV-023 — INTERRUPTED

`INTERRUPTED` means Athena cannot truthfully assert normal completion because execution was externally interrupted, normally by process death, runtime loss, or crash.

`INTERRUPTED` MAY be recoverable and is not equivalent to `FAILED`.

---

## BHV-024 — COMPLETE

`COMPLETE` means Athena has sufficient evidence that the task objective and all mandatory acceptance criteria have been satisfied.

---

# 9. Agent Iteration Behavior

Every normal reasoning iteration MUST preserve the conceptual sequence:

```text
START_ITERATION
      ↓
ASSERT_RUNNABLE
      ↓
BUILD_CONTEXT
      ↓
SELECT_MODEL
      ↓
MODEL_REQUEST
      ↓
MODEL_RESPONSE
      ├──── capability calls
      │          ↓
      │      DISPATCH_CALLS
      │          ↓
      │      RECORD_RESULTS
      │          ↓
      │         loop
      │
      └──── no capability calls
                 ↓
         EVALUATE_TERMINATION
             │          │
            no         yes
             │          │
            loop     FINALIZE
```

Streaming, budgets, approvals, cancellation, failures, and crash recovery MUST NOT change this fundamental behavioral model.

---

# 10. Session Behavior

## BHV-025 — Durable transcripts

Accepted session messages MUST survive ordinary process restart.

---

## BHV-026 — Session resume

Resuming an existing session MUST restore enough canonical history to continue work without fabricating prior context.

At minimum:

* historical persisted messages remain available;
* prior task lineage remains available;
* durable task history remains intact.

This is a mandatory v1 acceptance behavior.

---

## BHV-027 — Single session authority

CLI, HTTP, ACP, or future interfaces MUST NOT maintain divergent canonical copies of session history.

---

## BHV-028 — Historical truth

Persisted session history records what actually occurred.

Context compression MUST NOT rewrite historical truth in storage merely to reduce model context.

---

# 11. Context Compilation

## BHV-029 — Bounded context

Each model request MUST receive a bounded context appropriate to the selected model.

Context selection MAY omit lower-value content but MUST retain required behavioral constraints.

---

## BHV-030 — Required context categories

The compiler MUST be capable of considering:

* runtime safety constraints;
* current user instruction;
* task objective;
* acceptance criteria;
* project instructions;
* recent session messages;
* current task state;
* relevant memory;
* retrieved older history;
* active skills;
* relevant artifacts;
* capability descriptions;
* model capability constraints;
* token budget.

---

## BHV-031 — Instruction authority

Instruction precedence MUST behave as:

```text
runtime safety constraints
        >
current explicit user instructions
        >
project instructions
        >
established session instructions
        >
activated skills
        >
retrieved informational context
        >
external/untrusted content
```

External content MUST NOT acquire instruction authority merely by containing imperative wording.

---

## BHV-032 — Compression protection

Context reduction MUST NOT casually discard or distort:

* current user constraints;
* pending approval decisions;
* active acceptance criteria;
* unresolved errors;
* workspace/security boundaries;
* relevant current mutation state.

Large or verbose lower-value data SHOULD instead be summarized, compacted, or artifactized.

---

## BHV-033 — Provenance preservation

Nontrivial injected context SHOULD remain attributable to its source.

Athena MUST preserve the distinction between authoritative instructions, user content, curated agent knowledge, external content, and untrusted material sufficiently for policy and context handling to act on that distinction.

---

# 12. Model Selection

## BHV-034 — Requirement filtering

Athena MUST NOT select a model which is known to lack a capability required by the current request when a compatible configured alternative is required for successful execution.

Routing may consider:

```text
requirements
capability support
privacy
configured priority
availability
cost
context capacity
```

Fallback MUST be deterministic and inspectable.

---

## BHV-035 — Provider neutrality

Observable task semantics MUST remain provider-neutral.

Changing providers MUST NOT alter Athena's canonical:

* task state machine;
* capability authorization semantics;
* session schema;
* event semantics;
* execution authorization path.

Provider-specific limitations MAY appear as capability/availability differences.

---

## BHV-036 — Failure ownership

Provider adapters MAY retry failures they understand.

The kernel MUST NOT indiscriminately retry arbitrary provider failures.

---

## BHV-037 — Privacy-sensitive fallback

If a fallback would cross a policy boundary concerning privacy or cost, Athena MUST:

* deny the fallback; or
* request approval; or
* use a fallback already explicitly authorized by policy.

It MUST NOT silently proceed.

---

## BHV-038 — Offline provider behavior

Under the `offline` profile:

```text
remote model calls = 0
remote MCP calls   = 0
telemetry calls    = 0
network access     = denied
```

This behavior is mandatory.

---

# 13. Capability Behavior

## BHV-039 — Canonical invocation

Native, plugin, and MCP model-callable actions MUST converge on a canonical capability invocation path.

---

## BHV-040 — Schema validation

Capability arguments MUST be validated before policy evaluation.

Malformed arguments MUST NOT reach the executor.

---

## BHV-041 — Resolved-effect policy

Policy MUST evaluate the concrete request, not merely the capability name.

The decision SHOULD consider:

* principal;
* task;
* capability;
* resolved arguments;
* workspace;
* resolved resource;
* backend;
* effect classes;
* existing approval grants;
* autonomy profile.

Thus:

```text
filesystem.write("/project/src/a.py")
```

and:

```text
filesystem.write("/etc/sudoers")
```

MAY correctly produce different decisions.

---

## BHV-042 — Policy decisions are observable

Each policy decision MUST be recordable with:

* decision;
* reason;
* matched rule where applicable;
* relevant request identity;
* available approval scopes where applicable.

---

## BHV-043 — Denial means no effect

When policy returns `DENY`:

* the capability MUST NOT execute;
* no requested mutation may occur;
* the denial MUST be observable to the agent;
* the denial MUST be inspectable later.

---

# 14. Approval Behavior

## BHV-044 — Approval lifecycle

Approval requests MUST resolve to one of:

```text
APPROVED
DENIED
EXPIRED
CANCELLED
```

---

## BHV-045 — Single-use authorization

An approval identifier MUST NOT be replayable to authorize a different call.

Broader grants require an intentionally created broader scope.

---

## BHV-046 — Approval scopes

Athena MAY support approval scopes such as:

```text
call
task
session
project
profile
```

A broader scope MUST NOT be silently inferred from a narrower approval.

---

## BHV-047 — Approval binding

An approval MUST remain bound to enough context to prevent authorization confusion, including the relevant principal/resource/capability/effect scope.

---

# 15. Autonomy Profiles

Athena SHOULD expose the four baseline behavioral profiles validated by the research specification.

## BHV-048 — supervised

Default behavior SHOULD approximately be:

```text
local reads                allow
local execution            allow
writes                     ask
process side effects       ask
network reads              allow
secret access              ask
external writes            ask
destructive effects        ask or deny
```

---

## BHV-049 — coding

Behavior SHOULD approximately be:

```text
workspace reads/writes     allow
tests/builds               allow
package installation       ask
network reads              allow
arbitrary host paths       deny
external publication       ask
```

---

## BHV-050 — autonomous

Behavior SHOULD approximately be:

```text
isolated/container backend   default
workspace writes             allow
network                      profile-controlled
host credentials             explicit grants only
external side effects        ask or deny
```

---

## BHV-051 — offline

Behavior MUST prevent remote inference, remote MCP, telemetry, and network access unless the profile itself is explicitly changed.

---

# 16. Filesystem Behavior

## BHV-052 — Structured filesystem operations

Athena SHOULD provide structured filesystem behavior for at least:

```text
read
write
patch
list
stat
```

and MAY support:

```text
mkdir
copy
move
delete
```

without requiring separate top-level agent capabilities for every operation.

---

## BHV-053 — Workspace enforcement

Filesystem paths MUST be resolved against effective workspace policy before mutation.

Path traversal, symlink escape, and equivalent workspace-boundary bypasses MUST NOT produce unauthorized mutations.

---

## BHV-054 — Optimistic patch safety

A patch request MAY provide an expected content hash.

When the current file does not match the expected hash:

```text
FilesystemConflict / ConflictError
```

MUST occur.

Athena MUST NOT silently overwrite the externally changed file.

This behavior is explicitly required by the v1 acceptance scenarios.

---

## BHV-055 — Mutation recording

Structured filesystem mutation SHOULD generate a mutation record containing enough information to identify:

* resource;
* operation;
* before state/hash where known;
* after state/hash where known;
* reversibility;
* diff/snapshot artifact where available;
* approval reference where applicable;
* task/capability provenance.

---

# 17. Execution Behavior

## BHV-056 — Universal computation primitive

Athena MUST support model-requested local computation through an execution capability.

Required v1 runtimes:

```text
python
shell
```

Node and PowerShell are strongly recommended where platform support exists.

Only actually available runtimes SHOULD be advertised.

---

## BHV-057 — Streaming

Runtime output MUST be emitted incrementally.

Observable execution events SHOULD include:

```text
ExecutionStarted
StdoutChunk
StderrChunk
DisplayData
ArtifactProduced
ProcessSpawned
ResourceUsage
ExecutionExited
ExecutionTimedOut
ExecutionInterrupted
ExecutionFailed
```

Command output MUST stream.

---

## BHV-058 — Persistent runtime state

Persistent runtime state MUST default to task scope.

Example:

```text
Task A → Python session A
Task B → Python session B
```

State MUST NOT implicitly leak from one task's runtime session into another.

---

## BHV-059 — Persistent Python behavior

Within the same runtime session:

```python
x = 40
```

followed later by:

```python
print(x + 2)
```

MUST be capable of producing:

```text
42
```

without reconstructing state through the model.

---

## BHV-060 — Python process isolation

Generated Python MUST execute outside Athena's orchestration process.

A generated:

```python
sys.exit(...)
```

exception, crash, infinite loop, or interpreter failure MUST NOT directly terminate the Athena orchestration process.

---

## BHV-061 — Process ownership

Execution processes MUST have traceable ownership:

```text
Task
  └ RuntimeSession
       └ Execution
            └ ProcessTree
```

---

## BHV-062 — Process-tree cancellation

Cancelling an execution or owning task MUST terminate the owned process tree to the extent supported by the backend.

Orphan processes after normal task cancellation are a release-blocking defect.

---

## BHV-063 — Timeouts

When an execution exceeds its effective timeout:

* Athena MUST attempt to interrupt/terminate it;
* the result MUST indicate timeout;
* the timeout MUST be observable as an event/error;
* the task MUST decide its next step based on the timeout result rather than pretending execution succeeded.

---

## BHV-064 — Direct shell escape

The CLI SHOULD support:

```text
!command
```

to execute directly, record the result, and optionally attach it to current model context.

It SHOULD support:

```text
!!command
```

to execute directly and record the result without inserting output into LLM conversation context.

Both forms MUST pass through policy.

---

# 18. Artifact Behavior

## BHV-065 — Large result artifactization

Large outputs MUST NOT be injected wholesale into conversation state when artifactization is appropriate.

Athena SHOULD preserve:

```text
full content → ArtifactStore
bounded summary/excerpt + artifact ref → model context
```

The v1 behavior requires multi-megabyte command output to be stored as an artifact while only a bounded representation reaches the model.

---

## BHV-066 — Immutable references

Artifact identity SHOULD be content-addressed where practical.

Changing an artifact's contents MUST NOT silently preserve the same immutable content identity.

---

## BHV-067 — Artifact provenance

An artifact SHOULD remain attributable to its producer and task.

---

# 19. Mutation and Reversibility

## BHV-068 — Mutation ledger

Athena MUST be able to expose structured recorded mutations associated with a task.

---

## BHV-069 — Reversible mutations

If Athena marks a mutation `reversible`, sufficient preserved information MUST exist to perform the advertised inverse operation.

---

## BHV-070 — Irreversible effects

Athena MUST NOT label an operation generically reversible merely because it was initiated by Athena.

Examples that cannot receive a universal undo guarantee include:

```text
git push --force
database destructive operation
sending a message
publishing externally
arbitrary POST mutation
process termination with lost volatile state
package installation with hooks
form submission through computer control
```

The upstream research explicitly distinguishes a mutation ledger from a universal undo system.

---

# 20. Secrets and Credentials

## BHV-071 — Secret opacity

Credential values SHOULD be represented to the model by references or availability metadata rather than raw secret text.

---

## BHV-072 — No raw secret by default

Raw credentials MUST NOT appear in model context by default.

The v1 acceptance condition explicitly requires the secret value to remain absent unless authorized.

---

## BHV-073 — Credential resolution after policy

Actual credential values MUST be resolved only after applicable policy checks.

---

## BHV-074 — Credential lease visibility

When a runtime must receive an actual secret, Athena SHOULD create an explicit, scoped credential-use record or lease.

The grant SHOULD identify:

* credential;
* task;
* execution backend;
* scope;
* expiration.

---

# 21. Cancellation

## BHV-075 — Cancellation is cross-layer

Task cancellation MUST propagate, where applicable, to:

* active provider request;
* running capability;
* active runtime execution;
* owned process trees;
* child tasks.

---

## BHV-076 — Cancellation terminal state

After successful cancellation processing, the task MUST become `CANCELLED`.

---

## BHV-077 — No hidden continuation

A cancelled task MUST NOT later continue autonomous reasoning unless an explicit resume/restart operation creates a valid new runnable state according to task semantics.

---

## BHV-078 — Cancellation acceptance behavior

Cancelling a task with an active provider call, shell process tree, and child agent MUST result in:

```text
provider cancellation attempted
process tree terminated
child cancellation propagated
task state = cancelled
no owned orphan process
```

This is a required v1 scenario.

---

# 22. Crash and Recovery

## BHV-079 — Durable truth

Athena MUST survive process termination without retrospectively inventing successful work.

---

## BHV-080 — Crash during execution

If Athena dies while a command is active, after restart:

* the parent task MUST NOT be marked complete solely because it had been running;
* the execution MUST be marked interrupted, unknown, recovered, failed, or another truthful non-success state;
* recovery state MUST be inspectable.

---

## BHV-081 — Crash points

Recovery tests MUST cover failures at least around:

* task creation;
* model streaming;
* capability request creation;
* policy approval;
* capability start;
* filesystem mutation;
* runtime execution;
* mutation before event persistence;
* scheduler claim;
* child execution;
* terminal task commit.

Restart behavior MUST preserve honest state.

---

## BHV-082 — Ambiguous side effects

If Athena cannot prove whether an external side effect occurred before a crash, it MUST expose the uncertainty.

It MUST NOT blindly replay potentially non-idempotent external operations.

---

# 23. Budgets and Deadlines

## BHV-083 — Effective resource budget

A task MAY have bounded:

* model tokens;
* monetary cost;
* wall-clock time;
* model requests;
* capability calls;
* child tasks;
* parallelism;
* execution time.

Configured limits MUST be enforceable rather than advisory only.

---

## BHV-084 — Hierarchical accounting

Child usage MUST count against applicable parent/root budgets.

If a child consumes model budget, root usage MUST reflect that consumption.

---

## BHV-085 — Budget exhaustion

When a mandatory resource budget is exhausted, Athena MUST stop consuming that resource and produce a truthful terminal or suspended outcome.

It MUST NOT silently exceed the configured hard limit.

---

## BHV-086 — Deadline expiration

A hard deadline MUST prevent indefinite continued autonomous work after expiration.

Active execution SHOULD be cancelled where appropriate.

---

# 24. Delegation

## BHV-087 — Delegation creates tasks

Delegation MUST be implemented behaviorally as creation of child `TaskSpec` work.

It MUST NOT expose an independent subagent state machine with different core semantics.

The required flow is:

```text
AgentKernel
    ↓
delegate capability
    ↓
TaskSpec
    ↓
TaskManager
    ↓
Worker
    ↓
AgentKernel
```

---

## BHV-088 — Fresh child context

Child tasks MUST receive a newly compiled context.

They SHOULD NOT automatically inherit the parent's entire conversation transcript.

---

## BHV-089 — Explicit child authority

Child task creation MUST explicitly resolve relevant:

```text
context
filesystem scope
write permission
network permission
secrets
MCP capabilities
computer access
model policy
budget
```

---

## BHV-090 — No credential inheritance

A parent credential does not imply child credential access.

If:

```text
parent → GitHub credential
child  → no GitHub credential grant
```

the child MUST NOT be able to resolve that credential.

---

## BHV-091 — Delegation depth

Default delegation depth SHOULD be:

```text
1
```

meaning:

```text
root
 └ worker
```

Normal configured maximum SHOULD generally remain around `2`.

Deeper recursion SHOULD require explicit configuration.

---

## BHV-092 — Child result

A child MUST return structured task outcome information including:

* status;
* summary;
* evidence;
* artifacts;
* mutations;
* unresolved items;
* usage.

Parent context SHOULD normally receive this structured result rather than the child's complete transcript.

---

## BHV-093 — Parallel children

Athena MAY execute independent children concurrently.

Parallelism MUST remain within configured budget and concurrency limits.

---

## BHV-094 — Parent cancellation

Cancelling a parent SHOULD cancel active owned child tasks unless the task relationship explicitly defines detached execution.

Detached child execution is not required for v1.

---

# 25. Scheduling

## BHV-095 — Scheduler behavior

The scheduler MUST only:

```text
determine due occurrence
atomically claim occurrence
instantiate TaskSpec
enqueue Task
```

It MUST NOT directly invoke a model or contain an independent reasoning loop.

---

## BHV-096 — v1 triggers

v1 SHOULD support:

```text
once
interval
cron
```

Condition, filesystem, message, webhook, and event triggers MAY be added later.

---

## BHV-097 — Occurrence idempotency

The same scheduled occurrence MUST NOT create duplicate task executions.

A uniqueness boundary equivalent to:

```text
(job_id, scheduled_for)
```

MUST prevent duplicate claims.

---

## BHV-098 — Scheduler crash recovery

If Athena crashes after claiming an occurrence, restart MUST NOT create a second task for that same claimed occurrence.

---

# 26. Memory

## BHV-099 — Memory is not transcript

Athena MUST keep distinct:

```text
messages     = historical interaction truth
task state   = current work state
memory       = curated durable knowledge
skills       = reusable procedure
```

These MUST NOT collapse into one undifferentiated knowledge object.

---

## BHV-100 — Memory provenance

Durable memory SHOULD retain source references and confidence.

---

## BHV-101 — No speculative promotion

The model MUST NOT silently promote speculation to trusted durable memory.

---

## BHV-102 — Contradiction support

Memory behavior MUST allow later information to supersede or contradict previous memory without rewriting historical provenance.

---

## BHV-103 — Explicit deletion/correction

Durable memories MUST be capable of being corrected or removed through the memory authority.

---

# 27. Skills

## BHV-104 — Portable skill format

Athena MUST support portable `SKILL.md` skill conventions and MUST NOT require a second Athena-only manifest for ordinary portable skills.

---

## BHV-105 — Progressive disclosure

Athena MUST NOT inject the complete contents of every installed skill into every model request.

Expected behavior:

```text
skill metadata
      ↓
relevance selection
      ↓
selected SKILL.md
      ↓
specific references/scripts as needed
```

---

## BHV-106 — Untrusted skill content

Skill content MUST remain subject to instruction authority, workspace policy, and capability policy.

A malicious `SKILL.md` MUST NOT gain greater authority than its configured trust level.

---

## BHV-107 — Skill improvement

The active agent loop MAY propose a `SkillCandidate`.

It MUST NOT silently rewrite an active skill by default.

Promotion SHOULD pass through:

```text
candidate
   ↓
format validation
   ↓
security validation
   ↓
optional tests
   ↓
approval/policy
   ↓
new version
```

---

# 28. MCP Behavior

## BHV-108 — MCP capability normalization

MCP tools exposed to the model MUST enter Athena through the normal capability registry and policy path.

They MUST NOT receive privileged bypass semantics merely because they originate from MCP.

---

## BHV-109 — MCP policy parity

Equivalent native and MCP actions MUST be subject to the same authorization principles.

The v1 acceptance scenario explicitly requires MCP capability invocation to pass through normal policy and session result handling.

---

## BHV-110 — MCP name collision

Two MCP servers exposing the same display name MUST still produce distinct canonical capability identities.

Tool collision MUST NOT cause one server's capability to silently replace another.

---

## BHV-111 — MCP metadata is not authority

A remote MCP server claiming that an operation is read-only or safe MUST NOT by itself grant authorization.

Its annotations are metadata, not policy.

---

## BHV-112 — MCP resources

MCP resources SHOULD integrate with the context/artifact plane rather than being falsely represented as executable tools.

---

# 29. Plugin Behavior

## BHV-113 — Version declaration

Plugins MUST declare a supported Athena plugin API version.

An incompatible plugin MUST fail clearly rather than being loaded optimistically with undefined behavior.

---

## BHV-114 — No kernel monkey-patching contract

Modifying the reasoning kernel through unsupported monkey-patching MUST NOT be treated as a stable plugin API.

---

## BHV-115 — In-process trust disclosure

Athena documentation MUST make clear that an in-process Python plugin effectively executes with the Athena process's operating-system privileges.

Policy metadata alone is not process isolation.

---

# 30. Event Behavior

## BHV-116 — Events are canonical observations

Core execution state MUST be observable through structured events rather than requiring interface-specific callback semantics.

---

## BHV-117 — Monotonic task sequences

For a given task:

```text
event.sequence
```

MUST increase monotonically.

---

## BHV-118 — Replay

A consumer MUST be able to request events after a previously observed sequence.

Example:

```text
last observed = 120
request       = events after 120
result        = 121, 122, 123, ...
```

This enables SSE resume, reconnect, inspection, debugging, and trajectory export.

---

## BHV-119 — Immutable events

Persisted events MUST be immutable.

Corrections occur by appending new events/state, not rewriting historical events.

---

## BHV-120 — Duplicate delivery

Event consumers MUST tolerate duplicate delivery.

Side-effecting consumers MUST deduplicate using stable event identity.

---

## BHV-121 — Terminal event consistency

A terminal task event MUST agree with canonical task state.

Athena MUST NOT persist:

```text
TaskCompleted
```

while canonical task state is:

```text
FAILED
```

or vice versa.

---

# 31. Inspection

## BHV-122 — Causal inspection

`athena inspect TASK_ID` SHOULD expose a human-understandable causal account of task execution.

At minimum it SHOULD make inspectable:

* task objective and status;
* parent/child lineage;
* model selections and usage;
* context compilation;
* capability calls;
* policy decisions;
* approvals;
* runtime execution;
* artifacts;
* mutations;
* child tasks;
* failures;
* terminal reasoning/evidence.

The v1 acceptance criterion requires a complete understandable causal timeline.

---

## BHV-123 — Inspection after restart

Inspection MUST depend on durable state rather than only process-local logs.

A restarted Athena instance MUST still be able to inspect prior persisted tasks.

---

# 32. Errors

## BHV-124 — Structured failures

Core failures SHOULD map into structured error categories such as:

```text
ConfigurationError

TaskError
TaskBudgetExceeded
TaskDeadlineExceeded

ProviderError
ProviderAuthenticationError
ProviderRateLimitError
ProviderTimeout
ContextOverflow

CapabilityError
CapabilityUnavailable
CapabilityValidationError

PolicyDenied
ApprovalExpired

ExecutionError
ExecutionTimeout
ExecutionInterrupted
RuntimeUnavailable

FilesystemConflict

MCPError

Cancelled

PersistenceError
RecoveryError
```

---

## BHV-125 — User-facing explanation

A structured failure SHOULD retain a concise human-readable explanation without requiring users to understand internal exception classes.

---

## BHV-126 — Retry ownership

The subsystem which understands a transient failure SHOULD own its retry behavior.

Examples:

```text
provider HTTP timeout          → provider adapter
MCP transport interruption     → MCP adapter
SQLite busy                    → state layer
generated Python syntax error  → returned to agent as execution result
policy denial                  → never blind retry
```

---

## BHV-127 — Policy denial is not transient

Athena MUST NOT repeatedly retry the same denied operation hoping authorization semantics will change.

A materially changed request MAY be evaluated separately.

---

# 33. Completion and Verification

## BHV-128 — Candidate completion

A response indicating the model believes work is finished creates only a **candidate completion**.

---

## BHV-129 — Acceptance verification

When objective acceptance criteria exist, Athena SHOULD verify them before assigning `COMPLETE`.

Verification MAY include:

```text
command verification
file predicates
artifact predicates
structured result predicates
model judgment
human confirmation
```

Objective verification is preferred where available.

---

## BHV-130 — Verification evidence

The final result SHOULD retain sufficient evidence to explain why each acceptance criterion passed, failed, or remained unverified.

---

## BHV-131 — Failed criterion

If a mandatory criterion fails:

```text
Task status ≠ COMPLETE
```

The resulting state SHOULD normally be:

```text
PARTIAL
FAILED
BLOCKED
```

depending on why the criterion remains unsatisfied.

---

## BHV-132 — Weaker verification

When completion depends on subjective model judgment rather than objective evidence, Athena SHOULD make that weaker evidentiary status observable.

---

# 34. Final Task Result

A terminal `TaskResult` MUST be capable of representing:

```text
task_id
status
summary
evidence
artifacts
mutations
unresolved
usage
```

---

## BHV-133 — COMPLETE result

A complete result MUST describe the outcome and SHOULD expose material evidence/artifacts.

It SHOULD NOT present unresolved mandatory work as completed.

---

## BHV-134 — PARTIAL result

A partial result MUST clearly separate:

```text
completed
unresolved
failed verification
remaining work
```

---

## BHV-135 — BLOCKED result

A blocked result MUST identify the blocking dependency or permission.

---

## BHV-136 — FAILED result

A failed result MUST expose enough structured failure information to diagnose the failure without requiring raw internal stack traces.

---

## BHV-137 — CANCELLED result

A cancelled result MUST not be phrased as successful completion.

---

# 35. Interface Behavior

## BHV-138 — CLI

The CLI MUST expose the canonical task/session system rather than maintaining independent conversational state.

---

## BHV-139 — HTTP

HTTP endpoints MUST create, resume, inspect, cancel, or otherwise operate on the same canonical task/session records used by the CLI.

---

## BHV-140 — SSE

SSE streaming MUST use canonical events and support resume from an event sequence/cursor sufficient to avoid losing the logical event history after reconnection.

---

## BHV-141 — ACP

ACP MUST act as an adapter over Athena's existing task/session/event behavior.

ACP MUST NOT introduce:

* a second agent;
* a second session store;
* a second capability path;
* ACP-specific execution semantics.

---

## BHV-142 — Cross-interface continuity

A task created through one supported interface SHOULD be inspectable through another interface when authentication/authorization permits.

---

# 36. Platform Behavior

## BHV-143 — Semantic consistency

Core task, policy, persistence, event, and completion semantics MUST remain consistent across supported operating systems.

---

## BHV-144 — Platform truthfulness

Platform differences MUST be exposed rather than hidden.

Athena MUST NOT pretend:

```text
Bash == PowerShell
```

or expose unavailable runtimes as available.

The implementation should generate an availability map and expose only real runtime/capability availability.

---

# 37. Security Behavior

## BHV-145 — Path traversal

Attempts to escape permitted workspace scope through `..`, alternate separators, symlinks, or equivalent resolution tricks MUST NOT result in unauthorized filesystem access.

---

## BHV-146 — TOCTOU protection

Where Athena relies on an expected resource identity/hash for mutation safety, resource replacement between validation and mutation MUST be detected or safely constrained.

---

## BHV-147 — Approval replay protection

Reusing an approval for an unauthorized second operation MUST fail.

---

## BHV-148 — Child escalation prevention

A child MUST NOT expand its own policy scope beyond the authority granted in its `TaskSpec`.

---

## BHV-149 — Shell boundary consistency

A host path forbidden through the filesystem capability MUST remain forbidden through shell/runtime execution unless policy explicitly defines otherwise.

---

## BHV-150 — External prompt injection

External documents, web content, MCP resources, tool output, or repository data containing instructions MUST remain lower-authority context unless explicitly elevated through a trusted configuration path.

---

# 38. Privacy Behavior

## BHV-151 — Remote disclosure boundary

Before state is sent to a remote model, Athena SHOULD apply applicable privacy/policy filtering.

Conceptually:

```text
State
  ↓
ContextCompiler
  ↓
Privacy / policy filtering
  ↓
ModelRequest
  ↓
Remote provider
```

---

## BHV-152 — Secret filtering

Known credentials MUST NOT be automatically included in remote model requests.

---

## BHV-153 — Inspectability

Operators SHOULD be able to determine which provider/model received a model request and enough metadata to understand which context sources were selected.

Raw secret values SHOULD remain redacted.

---

# 39. Required v1 End-to-End Behaviors

The implementation specification declares the following scenarios as defining whether Athena v1 exists.

---

## Scenario A — Basic agent execution

### Given

The user requests:

```text
Create hello.py that prints "hello", then run it.
```

### Then Athena MUST

1. create a task;
2. compile context;
3. call a model;
4. receive filesystem/execution requests;
5. authorize them;
6. write the file;
7. record the mutation;
8. execute Python;
9. stream stdout;
10. persist results;
11. call the model again as necessary;
12. verify the result;
13. return a truthful terminal result.

---

## Scenario B — Persistent Python state

### Given

Same task/runtime session:

```python
x = 40
```

then:

```python
print(x + 2)
```

### Then

Output MUST include:

```text
42
```

---

## Scenario C — Policy denial

### Given

Workspace permits:

```text
/project/**
```

and the model requests:

```text
write /etc/sudoers
```

### Then

```text
decision = DENY
mutation = none
```

---

## Scenario D — Approval

### Given

A package installation requires approval.

### Then

```text
RUNNING
  ↓
WAITING_APPROVAL
  ↓ approval
RUNNING
```

and the approval is durably recorded.

---

## Scenario E — File conflict

### Given

1. Athena observes hash A.
2. Another actor modifies the file.
3. Athena attempts a patch expecting hash A.

### Then

```text
FilesystemConflict
```

and no silent overwrite occurs.

---

## Scenario F — Artifactization

### Given

Execution produces multi-megabyte output.

### Then

```text
full output → artifact
model context → bounded excerpt + reference
```

---

## Scenario G — Session resume

### Given

Athena is stopped and restarted.

### When

The existing session is resumed.

### Then

Historical persisted messages and task history remain available.

---

## Scenario H — Crash during execution

### Given

Athena is killed while a long-running shell command is active.

### Then after restart

```text
task != complete
execution = interrupted/unknown/recovered state
recovery state = inspectable
```

---

## Scenario I — Delegation

### Given

A parent creates two independent children.

### Then

Children receive:

* fresh context;
* bounded permissions;
* bounded budgets;
* isolated credentials unless explicitly granted.

They MAY execute concurrently.

Each returns a structured `TaskResult`.

---

## Scenario J — Hierarchical budget

### Given

A child consumes token or monetary budget.

### Then

Applicable root usage reflects that consumption.

---

## Scenario K — Scheduler idempotency

### Given

The same cron occurrence is observed twice because of crash/restart behavior.

### Then

Exactly one task is created for that occurrence.

---

## Scenario L — MCP capability

### Given

An MCP server exposes a model-callable tool.

### Then

* it appears through the capability registry;
* normal policy applies;
* its normalized result enters the session/task loop;
* it receives no policy bypass.

---

## Scenario M — MCP collision

### Given

Two servers both expose:

```text
search
```

### Then

Their canonical capability IDs remain distinct.

---

## Scenario N — Offline mode

### Then

```text
remote model requests = 0
remote MCP requests   = 0
telemetry             = 0
network access        = denied
```

---

## Scenario O — Secret protection

### Given

A credential is configured.

### Then

Its raw value does not enter model context unless specifically authorized.

---

## Scenario P — Child credential isolation

### Given

Parent:

```text
GitHub credential = granted
```

Child:

```text
GitHub credential = not granted
```

### Then

The child cannot resolve that credential.

---

## Scenario Q — Acceptance verification

### Given

Criterion:

```text
pytest must exit 0
```

and the model says:

```text
Done.
```

while tests fail.

### Then

```text
task != COMPLETE
```

and SHOULD be `PARTIAL` or `FAILED` according to the broader result.

---

## Scenario R — Event replay

### Given

The event client disconnects after sequence:

```text
120
```

### Then

A resumed subscription can obtain:

```text
121+
```

in canonical order.

---

## Scenario S — Task inspection

### When

```text
athena inspect TASK_ID
```

is invoked.

### Then

Athena presents a coherent causal timeline of the task.

---

## Scenario T — Cancellation

### Given

A task has:

* active model request;
* running shell process tree;
* active child task.

### When

The user cancels the task.

### Then

```text
provider cancellation attempted
process tree terminated
child cancellation propagated
task = CANCELLED
owned orphan process = none
```

## The implementation spec treats these scenarios as v1-defining behavior.

# 40. Additional Mandatory Security Scenarios

A conforming test suite MUST exercise at least:

```text
path traversal
symlink workspace escape
TOCTOU path replacement
write without approval
approval replay
secret leakage into model context
child secret inheritance
child capability escalation
MCP capability collision
false MCP safety annotation
malicious SKILL.md
external prompt injection
runtime process escape
orphan process
destructive recursive command
scheduler duplicate claim
artifact path escape
filesystem race conflict
unauthorized host path through shell
```

These scenarios are required by the implementation specification's security-test requirements.

---

# 41. Deterministic Behavioral Testing

## BHV-154 — Fake provider

Athena MUST support deterministic testing without live model inference.

A scripted fake provider SHOULD be able to express behavior such as:

```yaml
steps:
  - expect:
      user_contains: "create hello.txt"
    respond:
      capability_call:
        name: filesystem
        arguments:
          operation: write
          path: hello.txt
          content: hello

  - expect:
      capability_result:
        status: success
    respond:
      text: Done.
```

This allows deterministic tests of the kernel, context compiler, capability registry, policy engine, session store, event store, and task manager without external inference.

---

# 42. Runtime Contract Tests

Every runtime implementation MUST satisfy a common behavioral suite covering:

```text
start
stdout
stderr
exit code
working directory
environment
timeout
interrupt
process-tree cleanup
persistent state
reset
close
```

---

# 43. Backend Contract Tests

Every execution backend SHOULD prove consistent behavior for:

```text
filesystem visibility
working-directory mapping
environment forwarding
network policy
process cancellation
artifact transport
workspace isolation
```

---

# 44. Provider Contract Tests

Every provider implementation SHOULD prove normalized behavior for:

```text
text response
streaming
capability call
multiple capability calls
reasoning content
usage reporting
cancellation
provider failure
malformed response
context overflow
```

---

# 45. Capability Contract Tests

Every capability implementation MUST prove:

```text
schema validation
policy interception
event emission
normalized result
typed errors
cancellation where applicable
```

---

# 46. Behavioral Non-Goals

Athena v1 behavior does **not** require:

```text
GUI computer control
visual browser agent
voice
TTS
messaging gateway
Telegram
Discord
Slack
SSH execution
remote worker execution
serverless execution
vector database
automatic skill promotion
desktop GUI
full OpenAI-agent-compatible proxy
distributed swarm orchestration
```

These are explicitly outside the v1 product boundary.

Their absence MUST NOT be considered a failure of v1 conformance.

---

# 47. Behavioral Anti-Patterns

A conforming Athena implementation MUST NOT exhibit the following behavior.

## BAD-001 — Second reasoning loop

```text
Athena Agent
  ↓
embedded agent
  ↓
model
```

---

## BAD-002 — Policy bypass

```text
model
  ↓
shell
  ↓
host mutation
```

without policy evaluation.

---

## BAD-003 — Interface-dependent truth

```text
CLI says task complete
HTTP says task running
database says task failed
```

for the same canonical task state.

---

## BAD-004 — Silent overwrite

External file changes are overwritten despite an expected-hash mismatch.

---

## BAD-005 — Secret inheritance

A delegated child can access every parent credential merely because its parent can.

---

## BAD-006 — Global REPL leakage

Task B can observe Task A's Python variables without explicit shared-session configuration.

---

## BAD-007 — Silent fallback escalation

A local/private/free model fails and Athena silently switches to remote/less-private/paid inference.

---

## BAD-008 — Fake recovery

Athena crashes in the middle of an uncertain operation and later declares it successful without evidence.

---

## BAD-009 — Completion by assertion

The model says:

```text
Done.
```

therefore Athena sets:

```text
COMPLETE
```

despite failing acceptance criteria.

---

## BAD-010 — Scheduler reasoning

The scheduler independently invokes a model and conducts autonomous work without creating a normal task.

---

## BAD-011 — MCP trust escalation

An MCP server labels a tool read-only, therefore Athena automatically authorizes it.

---

## BAD-012 — Universal undo promise

Athena tells the user every action can be reverted even when external effects cannot be reversed.

---

# 48. Release-Blocking Behavioral Defects

The following defects are release-blocking for the relevant feature:

1. capability execution without policy interception;
2. illegal task state transition;
3. terminal event inconsistent with terminal task state;
4. unauthorized workspace escape;
5. raw secret leakage to model context without authorization;
6. child privilege or credential escalation;
7. orphan process remaining after successful task cancellation;
8. duplicate scheduled task for one canonical scheduled occurrence;
9. silent expected-hash filesystem overwrite;
10. task reported complete despite failed mandatory acceptance criteria;
11. offline mode making remote/network requests;
12. loss of persisted session/task truth during normal restart;
13. crash recovery inventing completion;
14. provider fallback silently crossing configured privacy/cost boundary;
15. cross-task implicit runtime-state leakage.

## The upstream implementation spec separately identifies policy bypass, illegal transitions, process ownership, child inheritance, terminal-state consistency, and silent fallback as architectural invariants.

# 49. Behavioral Definition of Done

A feature is not behaviorally complete merely because its happy path works.

For every applicable subsystem, conformance requires:

```text
normal behavior
typed failure behavior
event behavior
policy behavior
cancellation behavior
persistence behavior
crash behavior
inspection behavior
contract tests
security tests
```

The implementation specification likewise requires protocol implementation, typed failures, events, unit and contract tests, cancellation where relevant, persistence behavior where relevant, and documentation.

---

# 50. Athena v1 Behavioral Definition

Athena v1 exists when a user can give it meaningful machine work such as:

```text
Inspect this repository.
Find the defect.
Modify the necessary files.
Run the tests.
Continue based on their result.
Verify that the requested outcome is actually satisfied.
Report what changed and what remains unresolved.
```

and Athena can perform that task while preserving all of the following:

```text
one reasoning loop
durable task identity
durable session history
provider-neutral reasoning semantics
bounded context
explicit authorization
structured filesystem mutation
stateful code execution
streaming output
task-scoped runtime state
mutation provenance
artifact handling
acceptance verification
cancellation
crash truthfulness
bounded delegation
hierarchical budgets
scheduler idempotency
memory/skill separation
MCP policy parity
secret isolation
event replay
cross-interface consistency
inspectable causality
truthful terminal state
```

The architecture is successful only when those behaviors remain comprehensible as Athena grows.

The governing behavioral rule is therefore:

> **Athena may be autonomous, but it must never become ambiguous about who decided, who authorized, what acted, what changed, what evidence exists, or whether the requested work was actually completed.**
