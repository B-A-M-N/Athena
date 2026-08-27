# Athena

## Convergence, Integration & Build Specification

**Document:** `IMPLEMENTATIONSPEC.md`
**Status:** Normative implementation specification
**Project:** Athena
**Upstream documents:** `SPEC.md`, `RESEARCHSPEC.md`
**Audience:** Core maintainers, implementers, reviewers, security engineers, plugin/runtime authors
**Implementation target:** Python 3.12+
**Primary platforms:** Linux, macOS, Windows
**Primary operating mode:** Local-first, headless-first, asynchronous
**Primary interface:** CLI
**Secondary interfaces:** HTTP/SSE, ACP
**Optional external packages:** TUI, computer control, browser automation, gateway/channel adapters
**Core architectural requirement:** Exactly one authoritative reasoning loop

---

# 1. Purpose of This Document

`SPEC.md` defines Athena's intended product and conceptual architecture.

`RESEARCHSPEC.md` validates that architecture against Hermes Agent, Open Interpreter Classic, contemporary interoperability standards, execution/security requirements, and realistic implementation complexity.

This document exists to answer the question neither upstream document should answer independently:

> **Exactly what should engineers build, in what order, behind which boundaries, using which contracts, and which interpretation wins when the product spec and research findings differ?**

This document is therefore the implementation authority.

The project documentation hierarchy is:

```text
SPEC.md
    │
    │ defines product intent
    ▼
RESEARCHSPEC.md
    │
    │ validates, corrects, constrains
    ▼
IMPLEMENTATIONSPEC.md
    │
    │ resolves and makes normative
    ▼
code + tests
```

Where documents conflict:

```text
IMPLEMENTATIONSPEC.md
        >
RESEARCHSPEC.md
        >
SPEC.md
```

The code MUST conform to this document.

Changes to fundamental architecture MUST modify this document before or alongside the implementation.

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

`MUST` requirements are release-blocking.

`SHOULD` requirements may be deviated from only when the implementation contains an explicit justification.

`MAY` requirements are optional.

---

# 3. Athena's Final Definition

Athena is:

> **A compact, local-first task and autonomous-agent runtime in which one authoritative reasoning kernel operates over durable provenance-aware context, selects models through provider-neutral policy, and requests effect-scoped capabilities through an auditable authorization boundary, with stateful code execution providing the universal machine-action substrate.**

It combines:

```text
Hermes-derived concepts
    durable sessions
    memory
    skills
    delegation
    scheduling
    MCP
    provider independence
    resumability
    multi-interface invocation

with

Open Interpreter Classic-derived concepts
    universal execution
    persistent REPLs
    shell access
    local machine execution
    execution streaming
    human approval
    optional computer control
```

Athena does **not** combine their implementations wholesale.

Athena MUST NOT contain:

```text
Hermes Agent embedded inside Athena

or

Open Interpreter embedded inside Athena
```

The project is a clean architectural implementation.

---

# 4. Governing Architectural Equation

Athena's internal architecture MUST preserve:

```text
decision authority
    !=
authorization authority
    !=
execution authority
    !=
persistence authority
```

Specifically:

```text
AgentKernel
    decides

PolicyEngine
    authorizes

Capability + Execution layers
    act

State repositories
    record
```

No subsystem may silently take responsibility belonging to another authority.

This rule is more important than individual class names.

---

# 5. Absolute Architectural Invariants

The following invariants are non-negotiable.

## INV-001 — One reasoning loop

There MUST be exactly one implementation responsible for iterative:

```text
context
→ inference
→ action
→ observation
→ inference
```

That implementation is `AgentKernel`.

A delegated child agent MUST execute using the same `AgentKernel`.

A scheduler MUST NOT contain an agent loop.

ACP MUST NOT contain an agent loop.

MCP MUST NOT contain an agent loop.

Computer control MUST NOT contain an agent loop.

Interfaces MUST NOT contain an agent loop.

---

## INV-002 — One task abstraction

All autonomous work MUST ultimately become a `Task`.

The following are not separate orchestration architectures:

```text
interactive turn
delegated work
scheduled job
HTTP task
ACP request
gateway request
automation
```

They become:

```text
TaskSpec
    ↓
Task
    ↓
AgentKernel
```

---

## INV-003 — One session authority

Conversation/session state MUST have one durable authority.

CLI, HTTP, ACP, and future gateways MUST NOT maintain independent canonical session stores.

---

## INV-004 — One capability invocation path

Every model-requested external action MUST pass through:

```text
CapabilityRegistry
      ↓
PolicyEngine
      ↓
Capability executor
```

No direct bypass is allowed.

---

## INV-005 — One execution authority

All process execution initiated by the agent MUST flow through `ExecutionManager`.

Application modules MUST NOT casually call:

```python
subprocess.run(...)
asyncio.create_subprocess_exec(...)
os.system(...)
```

outside the execution subsystem.

Exceptions MAY exist for:

```text
installer/bootstrap
self-diagnostics
test fixtures
development tooling
```

but MUST NOT become part of agent execution semantics.

---

## INV-006 — Provider neutrality

`AgentKernel` MUST NOT contain provider-specific branches.

Forbidden:

```python
if provider == "anthropic":
    ...

if provider == "openai":
    ...
```

Provider-specific translation belongs inside provider adapters.

---

## INV-007 — Interface neutrality

`AgentKernel` MUST NOT know whether a request originated from:

```text
CLI
ACP
HTTP
TUI
Telegram
Discord
```

Interface metadata MAY travel with a task but MUST NOT affect kernel architecture.

---

## INV-008 — Policy cannot be bypassed by execution

A workspace path policy implemented in the filesystem capability MUST also constrain shell/runtime execution.

Example forbidden design:

```text
filesystem.write:
    /project only

bash:
    unrestricted host access
```

unless the selected autonomy profile explicitly grants that difference.

---

## INV-009 — No universal undo claims

Athena MUST distinguish:

```text
reversible mutation

from

irreversible external side effect
```

The product MUST NOT promise generic undo for arbitrary shell commands or external actions.

---

## INV-010 — No silent privacy escalation

Model fallback MUST NOT silently move:

```text
local → remote

or

private provider → less private provider

or

free model → paid model
```

unless policy explicitly authorizes that transition.

---

# 6. Explicit Non-Goals

Athena v1 is not intended to be:

```text
a giant agent framework

a LangChain replacement

a workflow DAG platform

a multi-tenant SaaS control plane

a Kubernetes-native orchestration system

a distributed swarm framework

a universal browser automation suite

a desktop GUI framework

a provider aggregation service

a vector-database product

a messaging-platform suite

a computer-use research framework
```

Athena MUST prioritize capability density over integration count.

---

# 7. System Boundaries

The final top-level architecture is:

```text
                                CLIENTS

                    CLI          ACP          HTTP
                     │            │            │
                     └────────────┼────────────┘
                                  ▼
                         ┌─────────────────┐
                         │  AthenaService  │
                         └────────┬────────┘
                                  │
                                  ▼
                         ┌─────────────────┐
                         │   TaskManager   │◄──── Scheduler
                         └────────┬────────┘
                                  │
                                  ▼
                         ┌─────────────────┐
                         │   AgentKernel   │
                         │                 │
                         │ compile context │
                         │ select model    │
                         │ infer           │
                         │ dispatch        │
                         │ terminate       │
                         └───────┬─────────┘
                                 │
                ┌────────────────┴────────────────┐
                ▼                                 ▼
       ┌──────────────────┐             ┌────────────────────┐
       │ ContextCompiler  │             │ CapabilityRegistry │
       └────────┬─────────┘             └──────────┬─────────┘
                │                                  │
        ┌───────┼─────────────┐                    ▼
        ▼       ▼             ▼             ┌──────────────┐
     Session  Memory        Skills          │ PolicyEngine │
        │       │             │             └──────┬───────┘
        └───────┼─────────────┘                    │
                ▼                                  ▼
          ArtifactStore                Capability execution
                                                   │
                    ┌──────────────────────────────┼────────────┐
                    ▼                              ▼            ▼
               Filesystem                       MCP         Execute
                                                               │
                                                               ▼
                                                      ExecutionManager
                                                       /             \
                                                      ▼               ▼
                                                   Local          Container
                                                      │
                                           ┌──────────┼──────────┐
                                           ▼          ▼          ▼
                                        Python      Shell      Node/PS
```

---

# 8. Package Dependency Rules

Dependencies MUST flow toward abstractions rather than implementations.

Recommended package layers:

```text
protocol
    ↑
core services
    ↑
application service
    ↑
interfaces
```

More concretely:

```text
athena.protocol
    ↑
athena.kernel
athena.context
athena.models
athena.capabilities
athena.execution
athena.policy
athena.state
athena.memory
athena.skills
athena.tasks
    ↑
athena.service
    ↑
athena.cli
athena.api
athena.acp
```

`athena.protocol` SHOULD depend primarily on:

```text
Python standard library
typing
dataclasses or lightweight schema library
```

It SHOULD NOT import implementation packages.

---

# 9. Recommended Repository Structure

```text
athena/
├── pyproject.toml
├── README.md
├── SPEC.md
├── RESEARCHSPEC.md
├── IMPLEMENTATIONSPEC.md
├── AGENTS.md
├── LICENSE
│
├── src/
│   └── athena/
│       ├── protocol/
│       │   ├── messages.py
│       │   ├── events.py
│       │   ├── tasks.py
│       │   ├── models.py
│       │   ├── capabilities.py
│       │   ├── execution.py
│       │   ├── policy.py
│       │   ├── artifacts.py
│       │   └── errors.py
│       │
│       ├── kernel/
│       │   ├── kernel.py
│       │   ├── lifecycle.py
│       │   ├── dispatch.py
│       │   └── termination.py
│       │
│       ├── tasks/
│       │   ├── manager.py
│       │   ├── worker.py
│       │   ├── delegation.py
│       │   ├── budgets.py
│       │   └── cancellation.py
│       │
│       ├── context/
│       │   ├── compiler.py
│       │   ├── instructions.py
│       │   ├── selection.py
│       │   ├── provenance.py
│       │   └── compression.py
│       │
│       ├── models/
│       │   ├── router.py
│       │   ├── registry.py
│       │   └── providers/
│       │       ├── openai_compat.py
│       │       ├── anthropic.py
│       │       └── fake.py
│       │
│       ├── capabilities/
│       │   ├── registry.py
│       │   ├── dispatcher.py
│       │   ├── filesystem.py
│       │   ├── execute.py
│       │   ├── memory.py
│       │   ├── skills.py
│       │   └── delegate.py
│       │
│       ├── execution/
│       │   ├── manager.py
│       │   ├── backend.py
│       │   ├── local.py
│       │   ├── container.py
│       │   ├── process_tree.py
│       │   └── runtimes/
│       │       ├── base.py
│       │       ├── python.py
│       │       ├── shell.py
│       │       ├── powershell.py
│       │       └── node.py
│       │
│       ├── policy/
│       │   ├── engine.py
│       │   ├── rules.py
│       │   ├── approvals.py
│       │   ├── credentials.py
│       │   └── profiles.py
│       │
│       ├── state/
│       │   ├── database.py
│       │   ├── sessions.py
│       │   ├── messages.py
│       │   ├── tasks.py
│       │   ├── events.py
│       │   ├── approvals.py
│       │   ├── mutations.py
│       │   ├── schedules.py
│       │   └── migrations/
│       │
│       ├── artifacts/
│       │   ├── store.py
│       │   ├── refs.py
│       │   └── cleanup.py
│       │
│       ├── memory/
│       │   ├── store.py
│       │   ├── retrieval.py
│       │   ├── candidates.py
│       │   └── conflicts.py
│       │
│       ├── skills/
│       │   ├── loader.py
│       │   ├── selector.py
│       │   ├── candidates.py
│       │   ├── validator.py
│       │   └── lifecycle.py
│       │
│       ├── mcp/
│       │   ├── client.py
│       │   ├── adapter.py
│       │   ├── tools.py
│       │   └── resources.py
│       │
│       ├── scheduler/
│       │   ├── scheduler.py
│       │   ├── triggers.py
│       │   └── claims.py
│       │
│       ├── service/
│       │   └── service.py
│       │
│       ├── cli/
│       │   ├── app.py
│       │   ├── chat.py
│       │   └── inspect.py
│       │
│       ├── api/
│       │   ├── app.py
│       │   └── sse.py
│       │
│       └── acp/
│           └── adapter.py
│
├── tests/
│   ├── unit/
│   ├── contract/
│   ├── integration/
│   ├── e2e/
│   ├── security/
│   └── crash/
│
└── examples/
```

Computer control SHOULD be a separate package:

```text
athena-computer
```

Messaging SHOULD be separate:

```text
athena-gateway
athena-channel-telegram
athena-channel-discord
...
```

---

# 10. Canonical Identity Types

All persistent objects SHOULD use opaque identifiers.

Recommended prefixes:

```text
task_
sess_
msg_
evt_
call_
exec_
run_
mem_
skill_
art_
mut_
apr_
job_
cred_
```

Example:

```text
task_01JZ...
```

UUIDv7 or another monotonic/time-sortable identifier SHOULD be used.

Database consumers MUST NOT depend on identifier internals.

---

# 11. Canonical Message Model

Athena MUST use a provider-neutral message representation.

```python
@dataclass(frozen=True)
class Message:
    id: str
    role: Role
    blocks: tuple[ContentBlock, ...]
    created_at: datetime
    provenance: Provenance
    metadata: Mapping[str, JSONValue]
```

`Role`:

```text
system
user
assistant
capability
```

Content block union:

```text
TextBlock
ReasoningBlock
ImageBlock
AudioBlock
FileRefBlock
CapabilityCallBlock
CapabilityResultBlock
ArtifactRefBlock
```

Provider schemas MUST be translated at adapter boundaries.

The persistent message schema MUST NOT assume:

```text
OpenAI tool calls
Anthropic blocks
Responses API items
Claude-specific reasoning
```

---

# 12. Provenance Model

Every nontrivial context block MUST retain provenance.

```python
@dataclass(frozen=True)
class Provenance:
    source_type: SourceType
    source_id: str | None
    trust: TrustClass
    scope: str | None
    created_at: datetime | None
```

Source types:

```text
system
user
session
task
memory
skill
project_instruction
file
artifact
MCP
web
runtime
capability
generated
```

Trust classes:

```text
authority
configured_instruction
user_content
agent_curated
external_content
untrusted
```

This distinction MUST survive context compilation.

---

# 13. AgentRequest

```python
@dataclass(frozen=True)
class AgentRequest:
    prompt: str

    session_id: str | None = None
    task_id: str | None = None

    workspace: WorkspaceSpec | None = None
    model_policy: ModelPolicy | None = None

    autonomy: AutonomyLevel = AutonomyLevel.SUPERVISED

    attachments: tuple[ArtifactRef, ...] = ()

    requested_capabilities: frozenset[str] | None = None

    metadata: Mapping[str, JSONValue] = field(
        default_factory=dict
    )
```

`AgentRequest` is an interface-level object.

It MUST be normalized into a `TaskSpec`.

---

# 14. TaskSpec

`TaskSpec` is the universal autonomous-work definition.

```python
@dataclass(frozen=True)
class TaskSpec:
    id: str

    objective: str
    acceptance_criteria: tuple[Criterion, ...]

    session_id: str | None
    parent_task_id: str | None

    context_refs: tuple[ContextRef, ...]

    workspace: WorkspaceSpec

    capability_policy: CapabilityPolicy
    model_policy: ModelPolicy

    resource_budget: ResourceBudget

    deadline: datetime | None

    delivery: DeliverySpec | None

    metadata: Mapping[str, JSONValue]
```

A `TaskSpec` MUST be immutable after task creation.

Runtime state belongs in the persisted `Task` record.

---

# 15. Task State Machine

Task states MUST be explicit.

```text
CREATED
   │
   ▼
QUEUED
   │
   ▼
RUNNING
   │
   ├─────────────► WAITING_APPROVAL
   │                    │
   │                    └────► RUNNING
   │
   ├─────────────► WAITING_INPUT
   │                    │
   │                    └────► RUNNING
   │
   ├─────────────► BLOCKED
   │
   ├─────────────► PARTIAL
   │
   ├─────────────► FAILED
   │
   ├─────────────► CANCELLED
   │
   ├─────────────► INTERRUPTED
   │
   └─────────────► COMPLETE
```

Terminal user-visible states:

```text
complete
partial
blocked
failed
cancelled
```

`interrupted` is a recovery state and MAY resume.

Illegal state transitions MUST be rejected.

---

# 16. AgentKernel Responsibility

The kernel owns only:

```text
task iteration

context compilation request

model selection request

model invocation coordination

model result interpretation

capability dispatch request

termination evaluation

event emission
```

The kernel MUST NOT implement:

```text
SQL
provider HTTP
filesystem operations
subprocess logic
MCP transport
cron calculations
memory ranking
skill parsing
approval storage
credential resolution
TUI rendering
```

---

# 17. Kernel Turn State Machine

Each kernel iteration follows:

```text
START_ITERATION
      │
      ▼
ASSERT_RUNNABLE
      │
      ▼
BUILD_CONTEXT
      │
      ▼
SELECT_MODEL
      │
      ▼
MODEL_REQUEST
      │
      ▼
MODEL_RESPONSE
      │
      ├─────────────── capability calls?
      │                         │
      │                        yes
      │                         ▼
      │                 DISPATCH_CALLS
      │                         │
      │                         ▼
      │                 RECORD_RESULTS
      │                         │
      │                         └─────── loop
      │
      └─────────────── no
                                │
                                ▼
                     EVALUATE_TERMINATION
                         │             │
                        no            yes
                         │             │
                         └── loop      ▼
                                    FINALIZE
```

---

# 18. Kernel Pseudocode

```python
async def run_task(task_id: str) -> TaskResult:
    task = await task_manager.acquire(task_id)

    while True:
        await task_manager.assert_runnable(task.id)

        await events.append(TaskIterationStarted(...))

        compiled = await context_compiler.compile(task)

        selection = await model_router.select(
            policy=task.model_policy,
            requirements=compiled.requirements,
        )

        response = await invoke_model(
            task=task,
            provider=selection.provider,
            model=selection.model,
            compiled=compiled,
        )

        if response.capability_calls:
            results = await dispatcher.dispatch_many(
                task=task,
                calls=response.capability_calls,
            )

            await session_store.append_results(
                task.session_id,
                results,
            )

            continue

        decision = await termination.evaluate(
            task=task,
            response=response,
        )

        if decision.terminal:
            return await task_manager.finalize(
                task,
                response,
                decision,
            )

        await session_store.append_assistant(
            task.session_id,
            response,
        )
```

Actual code MUST additionally support:

```text
streaming
cancellation
deadlines
budgets
approval suspension
provider failure
crash recovery
```

without obscuring this logical structure.

---

# 19. Resource Budgets

Resource accounting MUST be centralized.

```python
@dataclass(frozen=True)
class ResourceBudget:
    max_agent_iterations: int

    max_input_tokens: int | None
    max_output_tokens: int | None
    max_cost_usd: Decimal | None

    max_wall_time: timedelta | None

    max_children: int
    max_child_depth: int

    max_parallel_model_calls: int
    max_parallel_executions: int

    max_artifact_bytes: int
```

Children MUST consume from parent/root budget.

Budget accounting:

```text
root budget
   │
   ├ parent consumption
   ├ child A consumption
   ├ child B consumption
   └ verification consumption
```

A child MAY receive a local ceiling, but usage MUST roll up.

---

# 20. Cancellation Model

Cancellation MUST be hierarchical.

```text
Task cancellation
    ↓
cancel active model request
    ↓
cancel active capability calls
    ↓
interrupt owned executions
    ↓
cancel child tasks
```

Cancellation MUST be idempotent.

Calling cancel twice MUST NOT produce inconsistent state.

---

# 21. Deadlines

Tasks MAY define:

```text
deadline
max_wall_time
```

The earlier condition wins.

Runtime operations MAY define shorter local timeouts.

A timeout MUST NOT automatically be treated as:

```text
task failure
```

when another recovery strategy remains possible.

---

# 22. Backpressure

Athena MUST enforce limits for:

```text
concurrent tasks
model requests
execution jobs
child agents
event subscribers
artifact production
```

Concurrency SHOULD be controlled using shared async semaphores.

Unbounded:

```python
asyncio.create_task(...)
```

fanout is forbidden.

---

# 23. ModelProvider Protocol

```python
class ModelProvider(Protocol):

    async def list_models(
        self,
    ) -> Sequence[ModelInfo]:
        ...

    async def complete(
        self,
        request: ModelRequest,
    ) -> AsyncIterator[ModelEvent]:
        ...

    async def cancel(
        self,
        request_id: str,
    ) -> None:
        ...
```

Streaming is the canonical API.

There SHOULD NOT be two separate semantic implementations for:

```text
stream
non-stream
```

A consumer that wants a complete response can accumulate streamed events.

---

# 24. ModelInfo

```python
@dataclass(frozen=True)
class ModelInfo:
    id: str
    provider: str

    context_limit: int | None
    max_output_tokens: int | None

    tool_calling: bool
    vision: bool
    audio_input: bool
    audio_output: bool
    reasoning: bool
    structured_output: bool
    streaming: bool

    cost: CostInfo | None
    latency_class: str | None

    privacy_class: PrivacyClass
```

Router policy MUST depend on capability metadata rather than string hacks.

---

# 25. Model Roles

Athena MAY expose aliases:

```text
primary
fast
reasoner
reviewer
vision
summarizer
embedding
```

These are router policy aliases.

They MUST NOT instantiate separate classes of agent.

---

# 26. Model Routing

Example:

```python
ModelPolicy(
    role="primary",
    allowed=[
        "local/qwen",
        "anthropic/claude",
    ],
    require_tools=True,
    privacy="local-preferred",
    max_cost_usd=0.50,
)
```

Routing evaluation:

```text
requirements
    +
capability support
    +
privacy
    +
configured priority
    +
availability
    +
cost
    +
context capacity
```

Fallback MUST be deterministic and inspectable.

---

# 27. Provider Errors

Typed errors:

```text
ProviderUnavailable
ProviderAuthenticationError
ProviderRateLimited
ProviderTimeout
ProviderProtocolError
ProviderMalformedResponse
ContextOverflow
ModelUnavailable
RequestCancelled
```

Provider retries belong in provider adapters.

The kernel MUST NOT blanket-retry arbitrary provider failures.

---

# 28. Capability Protocol

```python
@dataclass(frozen=True)
class CapabilityDescriptor:
    id: str
    description: str

    input_schema: JSONSchema
    output_schema: JSONSchema | None

    effects: frozenset[EffectClass]

    tags: frozenset[str]

    origin: CapabilityOrigin
    version: str

    availability: Availability
```

---

# 29. Core Capability Set

Athena Standard SHOULD expose approximately:

```text
filesystem
execute
memory
skills
delegate
schedule
```

plus dynamically discovered MCP capabilities.

Optional packages MAY add:

```text
computer
browser
fetch
search
```

Avoid turning every operation into a separate top-level tool.

---

# 30. Capability Call

```python
@dataclass(frozen=True)
class CapabilityCall:
    id: str

    capability_id: str
    arguments: Mapping[str, JSONValue]

    task_id: str
    requested_by: Principal

    model_response_id: str | None
```

Arguments MUST be schema-validated before policy evaluation.

---

# 31. Capability Invocation Lifecycle

```text
REQUESTED
    │
    ▼
VALIDATED
    │
    ▼
POLICY_EVALUATION
    │
    ├── DENIED
    │
    ├── WAITING_APPROVAL
    │          │
    │          ├── DENIED
    │          └── APPROVED
    │
    ▼
STARTED
    │
    ├── FAILED
    ├── CANCELLED
    └── COMPLETED
```

Every transition SHOULD produce an event.

---

# 32. Effect Classes

```text
READ_LOCAL
WRITE_LOCAL

EXECUTE
SPAWN_PROCESS

NETWORK_READ
NETWORK_WRITE

SECRET_READ

DELETE
PRIVILEGED

EXTERNAL_MESSAGE
EXTERNAL_PUBLISH

COMPUTER_INPUT

FINANCIAL
```

Effect declarations are advisory metadata to aid policy.

They MUST NOT themselves grant authorization.

---

# 33. Policy Evaluation

Policy evaluates:

```text
principal
task
capability
resolved arguments
workspace
resource
backend
effect classes
existing approval grants
profile
```

It MUST NOT evaluate only capability names.

Example:

```text
filesystem.write("/project/src/a.py")
```

may be allowed while:

```text
filesystem.write("/etc/sudoers")
```

is denied.

---

# 34. Policy Result

```python
@dataclass(frozen=True)
class PolicyDecision:
    decision: Literal["allow", "ask", "deny"]
    reason: str
    matched_rule: str | None
    available_approval_scopes: tuple[ApprovalScope, ...]
```

Policy decisions MUST be logged.

---

# 35. Approval State Machine

```text
REQUESTED
    │
    ├── APPROVED
    │
    ├── DENIED
    │
    ├── EXPIRED
    │
    └── CANCELLED
```

Approval IDs MUST be single-use for the request they authorize unless a broader grant is created intentionally.

---

# 36. Approval Scopes

```text
call
task
session
project
profile
```

Avoid a vague scope called:

```text
always
```

`profile` explicitly indicates persistent policy.

---

# 37. Autonomy Profiles

Athena MUST ship at least:

```text
supervised
coding
autonomous
offline
```

## supervised

Default interactive profile.

Typical rules:

```text
read workspace                 allow
safe local inspection          allow
write workspace                ask
delete                         ask
package install                ask
privileged                     deny/ask
secret read                    ask
network read                   allow
network write                  ask
external messaging             ask
```

## coding

```text
workspace reads                allow
workspace writes               allow
build/test                     allow
package install                ask
delete workspace files         ask
outside-workspace writes       deny
external publish               ask
```

## autonomous

Recommended execution boundary:

```text
container
```

Host writes SHOULD be denied by default.

## offline

```text
remote models                  deny
remote MCP                     deny
network                        deny
telemetry                      deny
local inference                allow
local execution                allow per policy
```

---

# 38. Workspace Model

```python
@dataclass(frozen=True)
class WorkspaceSpec:
    id: str

    root: Path

    readable: tuple[PathRule, ...]
    writable: tuple[PathRule, ...]

    temp_root: Path

    execution_backend: str

    network_policy: NetworkPolicy
```

Filesystem capabilities and execution MUST both enforce the same workspace.

---

# 39. Filesystem Capability

Operations:

```text
read
write
patch
list
stat
mkdir
copy
move
delete
```

Structured filesystem operations SHOULD be preferred over shell mutations when they provide better:

```text
authorization
auditability
conflict detection
mutation capture
rollback
```

---

# 40. Optimistic File Modification

File modification SHOULD support expected content hashes.

Example:

```python
PatchFile(
    path="src/main.py",
    expected_sha256="abc123...",
    patch="..."
)
```

Mismatch:

```text
ConflictError
```

Athena SHOULD NOT silently overwrite changed files.

---

# 41. Mutation Ledger

```python
@dataclass(frozen=True)
class Mutation:
    id: str

    task_id: str
    capability_call_id: str | None

    resource: ResourceRef
    operation: str

    before_hash: str | None
    after_hash: str | None

    reversible: bool

    before_artifact: ArtifactRef | None
    diff_artifact: ArtifactRef | None

    approval_id: str | None

    created_at: datetime
```

---

# 42. Undo Semantics

`athena undo` MAY reverse structured mutations where before-state was captured.

Athena MUST explicitly report when an operation is irreversible.

Example:

```text
Reversible:
    filesystem.patch
    filesystem.write with snapshot
    move inside workspace

Not generically reversible:
    shell command
    remote API mutation
    package install
    process kill
    git push
    external message
    GUI form submit
```

---

# 43. ExecutionRequest

```python
@dataclass(frozen=True)
class ExecutionRequest:
    runtime: str
    source: str

    task_id: str
    workspace_id: str

    backend: str

    runtime_session_id: str | None

    persistence: RuntimePersistence

    cwd: str | None

    env: Mapping[str, str]

    stdin: bytes | None

    timeout: timedelta | None

    network_policy: NetworkPolicy | None
    resource_limits: ExecutionLimits | None

    metadata: Mapping[str, JSONValue]
```

Use `runtime`, not `language`.

---

# 44. Runtime Protocol

```python
class Runtime(Protocol):

    async def execute(
        self,
        request: ExecutionRequest,
    ) -> AsyncIterator[ExecutionEvent]:
        ...

    async def interrupt(
        self,
        execution_id: str,
    ) -> None:
        ...

    async def reset(
        self,
        runtime_session_id: str,
    ) -> None:
        ...

    async def close(
        self,
        runtime_session_id: str,
    ) -> None:
        ...
```

---

# 45. Execution Events

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

# 46. Initial Runtime Set

Required:

```text
python
shell
```

Strongly recommended:

```text
node
powershell
```

Availability is platform-dependent.

Athena MUST expose actual availability to the model.

---

# 47. Persistent Runtime Sessions

Persistent runtime state MUST default to task scope.

```text
Task A
    python session A

Task B
    python session B
```

They MUST NOT implicitly share state.

Explicit session attachment MAY be added later.

---

# 48. Python Runtime Isolation

Generated Python MUST NOT execute using:

```python
exec(...)
```

inside the Athena orchestration process.

Use a dedicated worker process.

Reasons:

```text
crash isolation
interruptibility
sys.exit isolation
resource limits
state reset
security boundary
```

---

# 49. Shell Runtime

Persistent shell MAY use a PTY-backed long-lived process.

Execution MUST track process groups.

Cancellation MUST terminate the process tree owned by the execution.

---

# 50. Process Ownership

```text
Task
  └ RuntimeSession
       └ Execution
            └ ProcessTree
```

A task cancellation MUST clean up owned process trees.

Orphan processes after task cancellation are a release-blocking defect.

---

# 51. Execution Backends

Core contract:

```python
class ExecutionBackend(Protocol):

    async def create_session(...)
    async def execute(...)
    async def interrupt(...)
    async def destroy_session(...)
```

Initial backends:

```text
local
container
```

Later:

```text
ssh
remote_worker
serverless
```

Do not implement future backends prematurely.

---

# 52. Direct Shell Escape

CLI SHOULD support:

```text
!command
```

meaning:

```text
execute directly
record result
optionally attach to current session context
```

And:

```text
!!command
```

meaning:

```text
execute directly
record result
do not inject output into LLM conversation
```

Both still pass through policy.

---

# 53. Artifact Architecture

Large outputs MUST move out of conversation messages.

Artifact storage MUST be content-addressable where practical.

Example:

```text
artifact://sha256/<digest>
```

Artifact metadata:

```text
id
hash
mime_type
size
storage_path
created_at
producer
task_id
metadata
```

---

# 54. Artifact Thresholds

The implementation SHOULD automatically artifactize:

```text
large command output
large MCP results
documents
screenshots
binary files
generated archives
large fetched pages
```

The model context should receive:

```text
summary
relevant excerpt
artifact reference
```

rather than the full blob.

---

# 55. ContextCompiler

`ContextCompiler` replaces the vague concept of a context manager.

Its job:

```text
durable state
+
current task
+
policy
+
knowledge
+
model limitations
        ↓
bounded provider-neutral model request
```

---

# 56. Context Inputs

```text
system/safety policy
current user request
TaskSpec
acceptance criteria
project instructions
recent messages
working task state
retrieved memory
session retrieval
relevant skills
selected artifacts
capability schemas
model capability constraints
token budget
```

---

# 57. Instruction Authority

Instruction precedence:

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

External content MUST NOT gain instruction authority simply because it contains imperative language.

---

# 58. Context Selection Dimensions

Do not use a single numeric priority.

Evaluate:

```text
authority
relevance
recency
token cost
mandatory status
```

Example:

```text
security constraint:
    old
    highly authoritative
    mandatory

80 KB test log:
    recent
    low authority
    removable
```

---

# 59. Context Compression

Default behavior:

```text
retain current instructions exactly

retain active acceptance criteria exactly

retain recent conversation

retrieve older relevant history

summarize low-value older history

compact verbose tool results

artifactize large outputs
```

The following MUST NOT be casually summarized away:

```text
current user constraints
pending approval decisions
active acceptance criteria
unresolved errors
workspace/security boundaries
current mutation state
```

---

# 60. AGENTS.md

Athena SHOULD support hierarchical `AGENTS.md`.

Resolver behavior:

```text
workspace root
      ↓
directories toward target path
      ↓
closest applicable AGENTS.md wins for conflicts
```

Athena MUST treat project instructions separately from general repository informational content.

---

# 61. Memory Architecture

Memory has two orthogonal dimensions.

Storage kind:

```text
working
episodic
semantic
procedural
```

Retrieval mode:

```text
always
searchable
ranked
explicit-only
```

---

# 62. MemoryRecord

```python
@dataclass(frozen=True)
class MemoryRecord:
    id: str

    content: str

    kind: MemoryKind
    scope: MemoryScope
    retrieval_mode: RetrievalMode

    subject: str | None
    tags: tuple[str, ...]

    source_refs: tuple[ContextRef, ...]

    confidence: float

    valid_from: datetime | None
    valid_until: datetime | None

    supersedes: tuple[str, ...]
    contradicted_by: tuple[str, ...]

    created_at: datetime
    updated_at: datetime
```

---

# 63. Memory Truthfulness

The model MUST NOT silently promote speculation to durable memory.

Memory candidate creation SHOULD capture:

```text
source
reason
confidence
subject
scope
```

Conflicts MUST be representable.

Example:

```text
mem_old:
    Python 3.11

mem_new:
    Python 3.13
    supersedes mem_old
```

---

# 64. Session History vs Memory

The following MUST remain distinct:

```text
Messages
    historical truth

Task state
    current work state

Memory
    curated durable knowledge

Skills
    reusable procedure
```

Do not build a generic "knowledge table" containing all four.

---

# 65. Skills Format

Athena MUST use portable `SKILL.md` conventions.

Directory:

```text
skill-name/
├── SKILL.md
├── scripts/
├── references/
└── assets/
```

Athena MUST NOT require an Athena-only secondary manifest for ordinary portable skills.

Athena-specific metadata SHOULD live under namespaced frontmatter or local database state.

---

# 66. Skills Lifecycle

Athena lifecycle:

```text
candidate
    ↓
draft
    ↓
validated
    ↓
active
    ↓
deprecated
    ↓
archived
```

Portable skill contents and local lifecycle state MUST remain separate concepts.

---

# 67. Skill Loading

Progressive disclosure:

```text
all skill metadata
        ↓
relevance selection
        ↓
selected SKILL.md
        ↓
specific references/scripts if needed
```

Athena MUST NOT inject all full skills every turn.

---

# 68. Skill Self-Improvement

The hot agent loop MAY produce:

```text
SkillCandidate
```

It MUST NOT immediately rewrite an active skill by default.

Candidate:

```python
@dataclass(frozen=True)
class SkillCandidate:
    source_task_id: str
    target_skill: str | None

    proposed_content_or_patch: str

    rationale: str
    evidence: tuple[ContextRef, ...]

    confidence: float
```

Promotion:

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

# 69. Delegation

Delegation is task creation.

Not:

```text
special subagent framework
```

Flow:

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

# 70. Child Isolation

Children MUST NOT inherit all parent privileges.

Explicitly determine:

```text
context
filesystem scope
write permissions
network
secrets
MCP tools
computer access
model policy
budget
```

---

# 71. Default Delegation Depth

Default:

```text
1
```

Meaning:

```text
root
 └ worker
```

Maximum recommended normal setting:

```text
2
```

Recursive delegation beyond this MAY exist but SHOULD require explicit configuration.

---

# 72. TaskResult

```python
@dataclass(frozen=True)
class TaskResult:
    task_id: str

    status: TaskStatus

    summary: str

    evidence: tuple[ContextRef, ...]

    artifacts: tuple[ArtifactRef, ...]
    mutations: tuple[MutationRef, ...]

    unresolved: tuple[str, ...]

    usage: UsageSummary
```

Parent context SHOULD generally receive structured result summaries rather than complete child transcripts.

---

# 73. Parallelism

Athena MAY execute independent children concurrently.

Supported patterns SHOULD initially be limited to:

```text
parallel independent work
serial dependency
map
review
```

Athena SHOULD NOT attempt to implement a general DAG language in v1.

---

# 74. Scheduler

Scheduler responsibility:

```text
determine when a job is due

atomically claim occurrence

instantiate TaskSpec

enqueue Task
```

It does not directly invoke an LLM.

---

# 75. ScheduledJob

```python
@dataclass(frozen=True)
class ScheduledJob:
    id: str

    trigger: TriggerSpec

    task_template: TaskTemplate

    timezone: str

    enabled: bool

    next_run_at: datetime | None
```

---

# 76. Trigger Types

v1:

```text
once
interval
cron
```

Later:

```text
condition
filesystem
message
webhook
event
```

---

# 77. Scheduler Idempotency

Duplicate execution of the same scheduled occurrence MUST be prevented.

Persist:

```text
job_id
scheduled_for
claim_id
task_id
```

Unique constraint:

```text
(job_id, scheduled_for)
```

---

# 78. Event Architecture

Events are the canonical observation API.

Do not use interface-specific callback forests.

---

# 79. Event Envelope

```python
@dataclass(frozen=True)
class Event:
    id: str

    task_id: str | None
    session_id: str | None

    sequence: int

    type: str

    schema_version: int

    timestamp: datetime

    payload: Mapping[str, JSONValue]
```

---

# 80. Event Ordering

For each task:

```text
sequence
```

MUST be monotonically increasing.

Consumers MUST be able to request:

```text
events after sequence N
```

This supports:

```text
SSE resume
TUI reconnect
ACP reconnect
inspection
debugging
trajectory export
```

---

# 81. Event Idempotency

Events are immutable.

Consumers MUST tolerate duplicate delivery.

Therefore events require stable IDs.

Side-effecting event consumers MUST deduplicate by event ID.

---

# 82. Event Categories

Core:

```text
TaskCreated
TaskQueued
TaskStarted
TaskStateChanged

ContextBuildStarted
ContextBuilt
ContextCompressed

ModelRequestStarted
ModelDelta
ModelReasoningDelta
ModelResponseCompleted
ModelRequestFailed

CapabilityRequested
CapabilityValidated
PolicyDecisionMade
ApprovalRequested
ApprovalResolved
CapabilityStarted
CapabilityProgress
CapabilityCompleted
CapabilityFailed

RuntimeSessionCreated
ExecutionStarted
StdoutChunk
StderrChunk
ExecutionExited
ExecutionInterrupted

ArtifactCreated
MutationRecorded

MemoryCandidateCreated
MemoryWritten

SkillActivated
SkillCandidateCreated

ChildTaskCreated
ChildTaskCompleted

TaskCompleted
TaskPartial
TaskBlocked
TaskFailed
TaskCancelled
TaskInterrupted
```

---

# 83. Durable Events vs Ephemeral Events

Not all stream events require permanent storage.

Durable:

```text
task transitions
model request boundaries
capability boundaries
policy decisions
approvals
mutations
artifacts
child task lifecycle
terminal results
```

Potentially ephemeral or compacted:

```text
individual model tokens
very small stdout chunks
animation/status events
```

Interfaces MAY receive richer ephemeral streams.

Durable history MUST retain enough information to reconstruct behavior.

---

# 84. Persistence

Default:

```text
SQLite
```

Required configuration:

```text
foreign_keys = ON
WAL mode
busy timeout
transaction boundaries
schema migrations
```

---

# 85. Database Tables

Minimum:

```text
schema_migrations

sessions
messages

tasks
task_relations

events

artifacts

runtime_sessions
executions

mutations

approvals
approval_grants

memories
memory_relations

skills
skill_versions

scheduled_jobs
job_runs

provider_usage
```

FTS:

```text
messages_fts
memories_fts
skills_fts
```

---

# 86. Transaction Boundaries

Critical transitions MUST be transactional.

Example scheduler claim:

```text
BEGIN

verify job occurrence not claimed

create claim

create task

update next_run

COMMIT
```

Task completion:

```text
BEGIN

persist terminal response

set task terminal state

append terminal event

COMMIT
```

---

# 87. Crash Recovery

At startup Athena MUST identify:

```text
RUNNING tasks without active worker

RUNNING executions without active process

claimed schedules without terminal run

WAITING_APPROVAL requests

persistent runtimes whose process no longer exists
```

These MUST NOT be reported as successful.

---

# 88. Recovery States

Use states such as:

```text
interrupted
recovery_required
```

Internal state MAY be richer than user-facing task statuses.

---

# 89. Recovery Policy

Some operations can resume:

```text
model request
    usually retryable at turn boundary

filesystem structured write
    inspect mutation state

persistent runtime
    recreate, but state may be lost

child task
    resume from persisted task state
```

Some operations cannot be proven safe to retry:

```text
external POST
email send
payment action
remote deployment
```

Those SHOULD become:

```text
blocked
```

or:

```text
recovery_required
```

rather than automatic retry.

---

# 90. MCP Integration

MCP is an integration adapter, not Athena's internal capability protocol.

```text
MCP server
   ↓
MCP adapter
   ↓
Athena representation
```

MCP tool:

```text
CapabilityRegistry
```

MCP resource:

```text
Context / Artifact subsystem
```

MCP prompt:

```text
command / prompt integration layer
```

---

# 91. MCP Namespacing

Canonical identity:

```text
mcp:<connection-id>:<tool-name>
```

A friendly alias MAY be shown to models.

Canonical identity MUST remain collision-free.

---

# 92. MCP Security

Server annotations are advisory.

They MUST NOT override Athena policy.

MCP servers SHOULD have trust metadata:

```text
trusted
configured
untrusted
```

Their outputs MUST receive provenance accordingly.

---

# 93. ACP Integration

ACP is a client interface around AthenaService.

```text
ACP client
   ↓
ACP adapter
   ↓
AthenaService
   ↓
TaskManager
   ↓
AgentKernel
```

There MUST NOT be an `ACPAgent` with independent reasoning behavior.

---

# 94. AthenaService

`AthenaService` is the interface-neutral application facade.

Suggested API:

```python
class AthenaService:

    async def submit_task(...)
    async def submit_message(...)
    async def resume_task(...)

    async def cancel_task(...)

    async def get_task(...)
    async def get_session(...)

    async def subscribe_events(...)

    async def respond_to_approval(...)
```

CLI, HTTP, ACP call this service.

---

# 95. HTTP API

Initial endpoints:

```text
POST /v1/tasks

GET  /v1/tasks/{task_id}

POST /v1/tasks/{task_id}/input

POST /v1/tasks/{task_id}/cancel

GET  /v1/tasks/{task_id}/events

GET  /v1/sessions
GET  /v1/sessions/{session_id}

GET  /v1/models
GET  /v1/capabilities

POST /v1/approvals/{approval_id}
```

---

# 96. Event Streaming

Prefer:

```text
SSE
```

for v1.

WebSocket SHOULD NOT be added unless bidirectional semantics materially justify the extra protocol complexity.

Client reconnect:

```text
Last-Event-ID
```

or equivalent sequence cursor.

---

# 97. CLI

Required commands:

```text
athena
athena chat
athena run
athena resume
athena inspect
athena sessions
athena tasks
athena models
athena memory
athena skills
athena jobs
athena mcp
athena config
athena doctor
athena serve
athena acp
```

---

# 98. Interactive Commands

```text
/model
/session
/context
/tasks
/memory
/skills
/permissions
/diff
/undo
/compact
/help

!command
!!command
```

These MUST call service/application APIs rather than directly editing state.

---

# 99. `athena inspect`

This command is a major acceptance requirement.

It MUST expose enough information to reconstruct the task.

Recommended sections:

```text
Task
    objective
    status
    parent
    workspace
    budget
    profile

Timeline
    ordered events

Model
    selections
    requests
    token use
    cost
    latency
    failures

Context
    included sources
    provenance
    token estimates
    omitted sources
    compression

Capabilities
    calls
    policy
    approvals
    results

Execution
    runtime sessions
    commands/code
    stdout/stderr refs
    exit statuses

Filesystem
    mutations
    diffs

Children
    task tree
    budgets
    results

Artifacts
    produced content
```

If `inspect` cannot answer:

> Why did Athena do this?

the architecture is insufficiently observable.

---

# 100. Secrets

Model context SHOULD contain secret references, not values.

```text
credential://github/default
```

Credential resolution occurs after authorization.

---

# 101. Credential Lease

When a runtime genuinely requires a credential:

```python
CredentialLease(
    credential_id=...,
    task_id=...,
    backend=...,
    expires_at=...,
)
```

Materialization MUST be scoped.

Lease creation SHOULD emit an event.

---

# 102. Secret Inheritance

Children MUST NOT automatically inherit parent credentials.

Credentials require explicit delegation grants.

---

# 103. Privacy Boundary

Remote providers receive only compiled, authorized context.

Forbidden:

```text
provider adapter reads database directly

provider adapter searches memory directly

provider adapter loads arbitrary files
```

Flow:

```text
State
  ↓
ContextCompiler
  ↓
Privacy/Policy filtering
  ↓
ModelRequest
  ↓
Provider
```

---

# 104. Computer Control

Computer control is not required in core v1.

Core SHOULD define protocol-compatible concepts.

Observation:

```text
Screenshot
AccessibilityTree
WindowList
FocusedElement
Clipboard
```

Action:

```text
PointerMove
PointerClick
PointerDrag
Scroll
KeyPress
Hotkey
TextInput
Wait
```

Preference:

```text
structured browser API
    >
accessibility tree
    >
visual screenshot interpretation
    >
absolute coordinates
```

Implement in:

```text
athena-computer
```

---

# 105. Plugin Architecture

Allowed extension categories:

```text
provider
runtime
capability
context source
memory provider
channel
event consumer
```

Forbidden extension concept:

```text
AgentKernel monkey patch
```

---

# 106. Plugin API Version

Plugin manifests MUST declare an Athena plugin API version.

Example:

```yaml
name: example-plugin
version: 1.3.0
athena-api: 1

provides:
  - capability

permissions:
  - network
```

Breaking plugin contract changes MUST increment the API version.

---

# 107. In-Process Plugin Trust

Python plugins loaded in-process effectively have the permissions of the Athena process.

Therefore policy metadata does not constitute isolation.

Documentation MUST say this explicitly.

Untrusted integrations SHOULD prefer:

```text
MCP
external processes
remote services
```

---

# 108. Configuration

Global:

```text
~/.config/athena/config.toml
```

Project:

```text
.athena/config.toml
```

Instructions:

```text
AGENTS.md
```

Skills:

```text
.agents/skills/
~/.agents/skills/
```

---

# 109. Configuration Precedence

Recommended:

```text
built-in defaults
    <
global config
    <
project config
    <
named profile
    <
explicit command invocation
```

Security policy MAY refuse unsafe overrides.

---

# 110. Configuration Philosophy

Expose product intent.

Good:

```toml
profile = "coding"
max_parallel_tasks = 4
execution_backend = "local"
context_strategy = "default"
```

Avoid implementation internals such as:

```toml
internal_queue_tick_ms = 173
summary_keep_left = 6
```

unless legitimately needed by advanced operators.

---

# 111. Structured Errors

Core error taxonomy:

```text
AthenaError

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

# 112. Retry Ownership

The layer understanding the failure owns retry.

Examples:

```text
HTTP timeout
    ProviderAdapter

MCP temporary transport failure
    MCP adapter

SQLite busy
    state layer

model-generated Python syntax error
    AgentKernel receives execution result

policy denial
    never blind retry
```

---

# 113. Completion Semantics

A model asserting:

```text
"Done."
```

is not sufficient evidence of task completion.

When acceptance criteria exist, candidate completion SHOULD trigger verification.

---

# 114. Acceptance Criteria

Criterion abstraction:

```python
@dataclass(frozen=True)
class Criterion:
    id: str
    description: str
    verification: VerificationSpec | None
    required: bool = True
```

Verification types MAY include:

```text
command
file predicate
artifact predicate
structured capability check
model judgment
manual
```

---

# 115. Completion Flow

```text
model proposes final answer
       ↓
candidate completion
       ↓
objective verification
       ↓
criteria status
       ↓
complete / partial / blocked / failed
```

Model-based verification SHOULD be treated as weaker evidence than deterministic checks.

---

# 116. Deterministic Fake Provider

A fake provider is REQUIRED.

It MUST support scripted responses.

Example:

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

This enables complete deterministic kernel tests.

---

# 117. Provider Contract Tests

Every provider MUST prove:

```text
text response

streaming

capability call

multiple capability calls

reasoning blocks where supported

usage reporting

cancellation

authentication error

rate limit

timeout

malformed response

context overflow
```

---

# 118. Runtime Contract Tests

Every runtime MUST prove:

```text
start
stdout
stderr
exit code
cwd
environment
stdin
timeout
interrupt
persistent state
reset
close
process cleanup
```

---

# 119. Backend Contract Tests

Every execution backend MUST prove:

```text
workspace mapping
filesystem visibility
cwd
environment
process cancellation
runtime persistence
artifact capture
network policy where supported
cleanup
```

---

# 120. Capability Contract Tests

Every capability MUST prove:

```text
schema validation
policy interception
event emission
result normalization
cancellation where applicable
typed errors
```

---

# 121. Security Tests

Required scenarios:

```text
path traversal

symlink workspace escape

TOCTOU path replacement

write without approval

approval reuse attack

secret leakage into model context

child secret inheritance

child capability escalation

MCP tool-name collision

MCP false safe annotation

malicious SKILL.md

external prompt injection

runtime child-process escape

orphan processes

destructive recursive command

scheduler duplicate claim

artifact path escape

filesystem race conflict

unauthorized host path through shell
```

---

# 122. Crash Testing

Artificially terminate Athena at:

```text
after task creation

during model streaming

after model requests capability

after policy approval

before capability start

mid-filesystem mutation

during runtime execution

after mutation before event persistence

after scheduled occurrence claim

during child execution

before terminal task commit
```

Restart behavior MUST preserve honest state.

---

# 123. Cross-Platform Requirements

Core semantics MUST remain consistent.

Platform differences SHOULD be exposed rather than hidden.

Linux/macOS:

```text
shell
python
node
```

Windows:

```text
powershell
python
node
```

Do not pretend Bash and PowerShell are interchangeable.

---

# 124. Platform Capability Detection

At startup Athena SHOULD generate an availability map:

```text
python       available
shell        available
node         unavailable
powershell   unavailable
container    available
```

Only available capabilities/runtimes are exposed to the model.

---

# 125. Async Architecture

Core runtime MUST be async-first.

Appropriate uses:

```text
provider streaming
event distribution
parallel children
runtime execution
scheduler
HTTP
MCP
```

Blocking operations MUST be isolated from the event loop.

---

# 126. Worker Architecture

Initial worker architecture:

```text
asyncio
+
SQLite
```

Do not require:

```text
Redis
Celery
RabbitMQ
Kafka
Kubernetes
```

A future external queue backend MAY be added behind `TaskQueue`.

---

# 127. Scheduler and Worker Separation

Logical components MAY initially run in one process.

Architecture must still distinguish:

```text
Scheduler
TaskQueue
Worker
AgentKernel
```

so they can separate later without changing semantics.

---

# 128. Persistence Isolation

Repositories own SQL.

Example:

```text
TaskRepository
EventRepository
SessionRepository
MemoryRepository
```

Kernel code MUST NOT issue raw SQL.

---

# 129. Schema Migrations

Every database schema change MUST have a migration.

Never rely on:

```text
delete local database and restart
```

as an upgrade strategy.

Migrations SHOULD be forward-only for production use.

Development tools MAY support reset.

---

# 130. Packaging

Initial Python distribution:

```text
athena-agent
```

or project-selected package name.

Optional extras MAY be:

```text
athena-agent[mcp]
athena-agent[anthropic]
athena-agent[container]
athena-agent[api]
```

Avoid making heavyweight integrations mandatory.

---

# 131. Dependency Policy

A dependency SHOULD be accepted when it:

```text
eliminates protocol maintenance
provides mature security-sensitive behavior
substantially reduces implementation burden
has compatible licensing
is actively maintained
```

A dependency SHOULD be rejected when Athena uses less than a trivial fraction of a huge framework.

---

# 132. Framework Avoidance

Athena SHOULD NOT adopt a heavyweight agent framework merely to save early development effort.

The architecture itself is the product.

---

# 133. Licensing Strategy

Preferred:

```text
permissive Athena codebase
```

Hermes concepts may be independently implemented.

Open Interpreter Classic's execution behavior should be treated as behavioral inspiration, not implementation source.

The project SHOULD maintain a clean implementation history.

---

# 134. Clean-Room Development Rules

When studying restrictive-license source:

Allowed:

```text
public behavior
documented interfaces
black-box behavior
architectural concepts
independently written tests
```

Avoid:

```text
copy/paste implementation bodies
mechanical translation of implementation
distinctive internal code structures
```

Maintain an upstream-notes document where necessary.

---

# 135. LOC Budget

Use budgets as architectural warning thresholds.

## Core contracts/kernel/task engine

```text
8k–15k LOC
```

## Useful local agent

```text
30k–50k LOC
```

## Athena Standard v1

Including:

```text
MCP
memory
skills
delegation
scheduler
HTTP
ACP
policy
SQLite
local/container execution
```

target:

```text
50k–80k production LOC
```

## Optional computer/browser packages

Separate budget.

The original 45k–70k complete-system target should be treated as aspirational rather than mandatory.

---

# 136. Complexity Hotspots

Expect disproportionate engineering effort in:

```text
cross-platform process lifecycle

persistent REPL correctness

cancellation

process-tree cleanup

context compilation

provider normalization

crash recovery

policy resolution

SQLite transactional correctness

MCP protocol evolution

artifact lifecycle

filesystem race safety

scheduler idempotency
```

These deserve stronger tests than ordinary utility modules.

---

# 137. Implementation Phases

The phases below are normative dependency order.

Features SHOULD NOT jump ahead merely because they are exciting.

---

# 138. Phase 0 — Architecture Contracts

Implement:

```text
IDs

Message
ContentBlock
Provenance

Event

TaskSpec
TaskResult
ResourceBudget

ModelProvider protocol

Capability protocol

Policy protocol

Execution protocol

WorkspaceSpec

ArtifactRef

typed errors
```

Also implement:

```text
FakeModelProvider
InMemory repositories
FakeCapability
```

Goal:

A deterministic synthetic AgentKernel can execute without any real model or shell.

Exit criteria:

```text
kernel unit tests deterministic
no implementation-specific dependencies in protocol
state transitions defined
events ordered
```

---

# 139. Phase 1 — Minimal Useful Agent

Implement:

```text
AgentKernel

TaskManager

Session repository

SQLite

OpenAI-compatible provider

CLI

filesystem read/write/patch

LocalBackend

shell runtime

python runtime

PolicyEngine

interactive approval

ArtifactStore

event stream
```

Required end-to-end demonstration:

```text
User:
    Inspect this project, fix a bug, run tests.

Athena:
    reads files
    edits files
    runs tests
    interprets result
    fixes if needed
    verifies
    reports
```

If this phase becomes architecturally messy, stop adding features.

Fix the core first.

---

# 140. Phase 2 — Durability

Add:

```text
task persistence
runtime persistence metadata
crash recovery
mutation ledger
task inspection
FTS session search
context compression
cancellation
budgets
```

Exit criteria:

```text
kill Athena during task
restart
state remains truthful
```

---

# 141. Phase 3 — Knowledge

Add:

```text
AGENTS.md

SKILL.md

skill selection

memory records

memory search

memory conflict/supersession

session retrieval
```

Do NOT add autonomous skill promotion yet.

---

# 142. Phase 4 — External Capability Ecosystem

Add:

```text
MCP client

MCP tools

MCP resources

plugin loading

Anthropic adapter
```

Exit criteria:

MCP capability behaves identically to native capability from the kernel's perspective.

---

# 143. Phase 5 — Orchestration

Add:

```text
delegation

TaskSpec child creation

shared hierarchical budgets

parallel independent children

scheduler
```

Exit criteria:

```text
parent delegates two bounded tasks
children execute concurrently
usage rolls up
children cannot escape permission scope
parent receives TaskResults
```

---

# 144. Phase 6 — Service Interfaces

Add:

```text
HTTP API
SSE

ACP adapter
```

Interfaces MUST reuse `AthenaService`.

No interface-specific persistence is allowed.

---

# 145. Phase 7 — Isolation

Add/harden:

```text
container execution

network restrictions

credential leases

resource limits

autonomous profile
```

Autonomous unsupervised scheduling SHOULD NOT be presented as production-safe before this phase.

---

# 146. Phase 8 — Computer Interaction

Separate package.

Add:

```text
observation
accessibility
screen capture
keyboard
mouse
structured browser
```

Computer control MUST obey the same capability/policy system.

---

# 147. Phase 9 — Learning

Add:

```text
MemoryCandidate extraction

SkillCandidate extraction

skill validation

skill tests

promotion workflow
```

This is intentionally last.

Do not make self-modification a dependency for normal agent competence.

---

# 148. Dependency Graph

```text
Protocol contracts
      │
      ▼
Kernel + TaskManager
      │
      ├───────────────┐
      ▼               ▼
Persistence        Fake provider
      │
      ▼
Local agent
      │
      ├───────────────┐
      ▼               ▼
Durability       Execution hardening
      │
      ▼
Knowledge
      │
      ▼
MCP/plugins
      │
      ▼
Delegation
      │
      ▼
Scheduler
      │
      ▼
HTTP/ACP
      │
      ▼
Container isolation
      │
      ▼
Computer
      │
      ▼
Learning
```

---

# 149. V1 Product Boundary

Athena v1 MUST include:

```text
one AgentKernel

TaskSpec

persistent sessions

SQLite + FTS5

provider-neutral message model

OpenAI-compatible provider

Anthropic provider

local OpenAI-compatible inference

shell execution

Python execution

persistent runtime sessions

filesystem read/write/patch

artifact storage

context compiler

AGENTS.md

portable skills

explicit memory

MCP tools/resources

bounded delegation

shared budgets

scheduler

scoped policy

approvals

mutation ledger

CLI

HTTP/SSE

ACP

task inspection

cancellation

crash-state recovery

deterministic fake provider
```

---

# 150. Explicitly Not Required for V1

```text
computer GUI control

visual browser agent

voice

TTS

messaging gateway

Telegram

Discord

Slack

SSH backend

remote workers

serverless execution

vector database

automatic skill promotion

desktop GUI

full OpenAI agent-compatible proxy

distributed swarm orchestration
```

---

# 151. V1 End-to-End Acceptance Scenarios

The following tests define whether Athena v1 exists.

---

## Scenario A — Basic agent execution

User:

```text
Create hello.py that prints "hello", then run it.
```

Athena MUST:

```text
create task
compile context
call model
receive filesystem/execute request
authorize
write file
record mutation
execute Python
stream stdout
store results
call model again
return success
```

---

## Scenario B — Persistent Python state

Model executes:

```python
x = 40
```

then:

```python
print(x + 2)
```

same runtime session.

Expected:

```text
42
```

---

## Scenario C — Policy denial

Model requests write:

```text
/etc/sudoers
```

while workspace only permits:

```text
/project/**
```

Expected:

```text
DENY
```

No mutation occurs.

---

## Scenario D — Approval

Model requests package installation.

Expected:

```text
task enters WAITING_APPROVAL

user allows call

task resumes

approval recorded
```

---

## Scenario E — File conflict

Athena reads file hash A.

External actor edits file.

Athena attempts patch expecting hash A.

Expected:

```text
ConflictError
```

No silent overwrite.

---

## Scenario F — Artifactization

A command generates multi-megabyte output.

Expected:

```text
full output stored as artifact

model receives bounded excerpt/reference
```

---

## Scenario G — Session resume

Stop Athena.

Restart.

Resume session.

Expected:

```text
historical messages available
task history intact
```

---

## Scenario H — Crash during execution

Kill Athena while long shell command is active.

Restart.

Expected:

```text
task not marked complete
execution marked interrupted or unknown
recovery state visible
```

---

## Scenario I — Delegation

Parent creates two children.

Expected:

```text
fresh child contexts

bounded permissions

bounded budgets

parallel execution

structured TaskResults
```

---

## Scenario J — Budget accounting

Child consumes token budget.

Expected:

```text
root usage reflects consumption
```

---

## Scenario K — Scheduler idempotency

Trigger same cron occurrence twice through crash/restart simulation.

Expected:

```text
exactly one task created
```

---

## Scenario L — MCP capability

Configure MCP server exposing tool.

Expected:

```text
tool appears through CapabilityRegistry

policy applies identically to native capability

call result enters session normally
```

---

## Scenario M — MCP name collision

Two servers expose:

```text
search
```

Expected canonical IDs remain unique.

---

## Scenario N — Offline mode

Start Athena offline.

Expected:

```text
zero remote model requests
zero remote MCP requests
zero telemetry requests
network denied
```

---

## Scenario O — Secret protection

Credential exists.

Expected:

```text
secret value not present in model context
```

unless explicitly authorized.

---

## Scenario P — Child credential isolation

Parent has GitHub credential.

Child receives no credential grant.

Expected:

```text
child cannot resolve credential
```

---

## Scenario Q — Acceptance verification

Task criterion:

```text
pytest must exit 0
```

Model claims success while tests fail.

Expected task status:

```text
partial or failed
```

not complete.

---

## Scenario R — Event replay

Disconnect event client at sequence:

```text
120
```

Reconnect.

Expected:

```text
events 121+ replay in order
```

---

## Scenario S — Task inspection

Run:

```text
athena inspect TASK_ID
```

Expected:

```text
full understandable causal timeline
```

---

## Scenario T — Cancellation

Cancel task with running shell tree and child agent.

Expected:

```text
provider cancelled

process tree killed

child task cancelled

task terminal state cancelled

no orphan process
```

---

# 152. Definition of Done for Core Components

A component is not complete merely because its happy path works.

Every component requires:

```text
protocol implementation

typed failures

events

unit tests

contract tests

cancellation where relevant

persistence behavior where relevant

documentation
```

---

# 153. Mandatory Architecture Tests

The repository SHOULD contain explicit invariant tests.

Examples:

```text
AgentKernel contains no provider imports.

AgentKernel contains no interface imports.

AgentKernel performs no raw SQL.

Capabilities cannot dispatch without PolicyEngine.

Execution only occurs through ExecutionManager.

Child tasks do not inherit credentials automatically.

Interfaces all invoke AthenaService.

No task can transition illegally.

Terminal task events match terminal database state.
```

---

# 154. Anti-Bloat Rules

## Rule 1

No second reasoning loop.

## Rule 2

No provider branch in AgentKernel.

## Rule 3

No interface branch in AgentKernel.

## Rule 4

No subprocess execution outside ExecutionManager for agent actions.

## Rule 5

No direct database writes outside repositories.

## Rule 6

No direct filesystem mutation from AgentKernel.

## Rule 7

No capability invocation bypassing PolicyEngine.

## Rule 8

No child permission inheritance by default.

## Rule 9

No raw secret in model context by default.

## Rule 10

No nonstandard skill format without demonstrated necessity.

## Rule 11

No global runtime REPL shared implicitly between tasks.

## Rule 12

No silent privacy/cost model fallback.

## Rule 13

No generic "undo" promise.

## Rule 14

No mandatory UI-specific state.

## Rule 15

No integration enters core merely because it is useful.

## Rule 16

No abstraction is accepted solely because it looks elegant.

It must remove a real coupling boundary.

---

# 155. Architecture Decision Records

Significant decisions SHOULD be recorded under:

```text
docs/adr/
```

Initial ADR set:

```text
ADR-001 One authoritative reasoning loop

ADR-002 TaskSpec as universal autonomous work unit

ADR-003 Provider-neutral internal message representation

ADR-004 CapabilityRegistry + PolicyEngine authorization path

ADR-005 ExecutionManager as exclusive execution authority

ADR-006 SQLite + artifact filesystem persistence

ADR-007 Content-addressed artifact references

ADR-008 Agent Skills-compatible skill format

ADR-009 Local/container initial execution backends

ADR-010 Hierarchical task budgets

ADR-011 SSE before WebSocket

ADR-012 Computer control outside core

ADR-013 Clean-room OI Classic execution reimplementation

ADR-014 No implicit child capability/secret inheritance
```

---

# 156. Code Review Checklist

Every major PR should answer:

```text
Does this introduce another decision authority?

Does this bypass policy?

Does it add provider-specific behavior to generic code?

Does it add interface-specific behavior to generic code?

Can this live as a plugin?

Can failure state be recovered?

What happens if cancellation occurs here?

What happens if the process dies here?

What durable event proves what happened?

What data is being sent remotely?

Can child tasks reach this resource?

Can this operation be reversed?

Is the schema migration defined?

Does athena inspect show this behavior?

What contract test proves alternate implementations remain compatible?
```

---

# 157. Architecture Review Triggers

Mandatory architecture review SHOULD occur when:

```text
core exceeds 25k LOC

standard production code exceeds ~80k LOC

single module exceeds ~1k LOC without justification

AgentKernel begins accumulating helper responsibilities

new execution backend requires kernel changes

new provider requires kernel changes

new interface requires kernel changes

new capability requires database schema changes unrelated to its state

child orchestration requires special-case kernel paths

plugin authors need internal monkey-patching

context logic becomes provider-specific
```

---

# 158. What SPEC.md Continues to Own

`SPEC.md` remains the authoritative description of:

```text
product identity

user-facing philosophy

broad desired capabilities

high-level architecture

why Athena exists

what Athena derives conceptually from Hermes and OI Classic
```

It is not expected to contain every low-level implementation contract.

---

# 159. What RESEARCHSPEC.md Continues to Own

`RESEARCHSPEC.md` remains the record of:

```text
external architectural validation

upstream behavior research

license research

standards validation

criticisms of initial assumptions

alternative analysis

evidence supporting architectural decisions
```

It is evidence and rationale.

It is not the day-to-day build order.

---

# 160. What IMPLEMENTATIONSPEC.md Owns

This document owns:

```text
precedence

normative contracts

final component boundaries

state transitions

build order

dependency relationships

release boundary

implementation constraints

security semantics

testing obligations

acceptance scenarios
```

When engineers ask:

> Which interpretation do we actually build?

the answer should be here.

---

# 161. Recommended Documentation Set

Athena should ultimately maintain:

```text
README.md
    quick identity / installation / use

SPEC.md
    product specification

RESEARCHSPEC.md
    architectural validation and research

IMPLEMENTATIONSPEC.md
    normative convergence/build specification

SECURITY.md
    threat model and trust boundaries

AGENTS.md
    instructions for coding agents working on Athena

docs/ARCHITECTURE.md
    developer-oriented implemented architecture

docs/PROTOCOL.md
    canonical data contracts

docs/STATE-MACHINES.md
    state transition reference

docs/PLUGIN-API.md
    extension API

docs/RUNTIME-API.md
    execution/runtime contract

docs/POLICY.md
    permissions and approvals

docs/STORAGE.md
    database and artifact persistence

docs/adr/
    architectural decision records
```

---

# 162. Initial Engineering Work Breakdown

Once Phase 0 begins, parallel work can be divided into six tracks.

```text
Track A
    protocol + schemas

Track B
    state + SQLite + migrations

Track C
    model adapters

Track D
    capability + policy system

Track E
    execution + runtimes

Track F
    kernel + task manager
```

Dependency rule:

```text
A must stabilize first.

B-E can then proceed mostly independently.

F integrates against contracts/fakes first.

Real implementations plug in afterward.
```

This minimizes early merge coupling.

---

# 163. First Milestone Repository State

Before any real provider or shell execution is merged, the project SHOULD already be able to run:

```text
Fake user request
      ↓
TaskSpec
      ↓
TaskManager
      ↓
AgentKernel
      ↓
Fake ContextCompiler
      ↓
Fake ModelProvider
      ↓
Fake Capability
      ↓
PolicyEngine
      ↓
Fake capability result
      ↓
second fake model response
      ↓
TaskResult
```

with:

```text
persistent events
deterministic tests
legal state transitions
```

If this cannot be done cleanly, the architecture is not ready for real capability complexity.

---

# 164. Second Milestone Repository State

Athena SHOULD then be able to:

```text
open a repository

read source

modify a file

run a test

observe failure

modify the file again

rerun test

return result

persist session

inspect entire timeline
```

using only:

```text
OpenAI-compatible model
filesystem
shell/python
SQLite
CLI
```

This is the first point where Athena is genuinely useful.

---

# 165. Third Milestone Repository State

Athena becomes durable when it can survive:

```text
user interruption

provider timeout

process timeout

Athena process crash

runtime crash

session resume
```

without lying about state.

Do not proceed aggressively into skills/delegation before this milestone is credible.

---

# 166. Fourth Milestone Repository State

Athena becomes extensible when:

```text
MCP capabilities

Anthropic

skills

memory

plugins
```

can be introduced without editing `AgentKernel`.

This proves the abstraction boundaries.

---

# 167. Fifth Milestone Repository State

Athena becomes autonomous when:

```text
delegation

scheduler

container isolation

shared budgets

scoped secrets

network policy
```

work together safely.

Autonomy is not defined merely as:

```text
"we turned approvals off"
```

---

# 168. Final Architectural Test

The strongest indicator Athena succeeded is not LOC.

It is whether these additions require changes to AgentKernel:

```text
new provider
new model
new MCP server
new runtime
new execution backend
new memory implementation
new interface
new channel
new computer backend
```

The desired answer is:

```text
No.
```

If adding these repeatedly changes the reasoning loop, Athena has recreated the architectural coupling it was designed to eliminate.

---

# 169. Final Implementation Principle

The project should optimize for this shape:

```text
small stable center
        +
replaceable edges
```

not:

```text
small version number
        +
rapidly expanding center
```

Athena's value comes from making the difficult boundaries explicit:

```text
reasoning
authorization
execution
state
knowledge
interfaces
```

while keeping their contracts narrow.

---

# 170. Governing Summary

Athena should be built as:

```text
ONE REASONING KERNEL
        │
        ▼
ONE UNIVERSAL TASK MODEL
        │
        ├──────── ContextCompiler
        │
        ├──────── ModelRouter
        │
        └──────── CapabilityRegistry
                       │
                       ▼
                  PolicyEngine
                       │
                       ▼
                 action systems
                    /       \
                   /         \
          structured tools   ExecutionManager
                                 │
                      ┌──────────┼──────────┐
                      ▼          ▼          ▼
                   Python      Shell     other runtimes
```

Around that center:

```text
SQLite
Artifacts
Memory
Skills
MCP
Scheduler
Delegation
CLI
HTTP
ACP
```

attach through stable interfaces.

The architectural synthesis is therefore not:

```text
Hermes Lite
+
Open Interpreter Classic
```

It is:

```text
Hermes-derived durable-agent semantics

plus

OI Classic-derived universal execution semantics

re-expressed through

Athena's stricter separation of
decision, authorization, execution,
state, and knowledge.
```

The success condition is not simply that Athena can do most of what Hermes and Open Interpreter can do.

The success condition is:

> **Athena retains the leverage of those systems without retaining the architectural coupling that made them large.**

That principle governs every implementation decision in this document.

---

# 171. Programmable Affordance Fabric

Athena MUST expose a governed effective affordance surface, not only a static
global capability registry. The effective surface is the composition of:

```text
global capabilities
    + project overlay
    + task overlay
    + user overlay
    + external capabilities
    + declarative workflows
    + selected skills and project knowledge
```

The fabric MUST preserve these boundaries:

```text
AgentKernel       decides
Workflow          deterministically composes
Capability        performs one governed operation
Skill/Memory      supplies knowledge
PolicyEngine      authorizes
ExecutionManager  executes
State stores      record
```

Task-local generated machinery MUST disappear at terminal task cleanup unless
explicitly promoted. Project and user promotion MUST be explicit, versioned,
provenance-bearing, and policy/approval controlled. SYSTEM promotion is a
normal Athena release operation and MUST NOT be autonomous.

Scratch programs MAY be cheap and ephemeral, but they MUST use the canonical
restricted execution path. Generated capabilities MUST carry code/schema
hashes, scope, dependencies, provenance, validation state, and proof/evidence
references. Declared effects are requests only: effective authority MUST be
calculated outside generated code and enforced by policy and the execution
backend.

Athena MUST expose a task-scoped generated-tool admission route. The route
MUST require a Task, validate source and schema before registration, return a
stable capability identity plus validation proof, and install only into that
Task's capability overlay. Promotion to project/user scope MUST be explicit;
creation MUST NOT silently replace a native or external capability.

Workflows MUST be declarative, inspectable, persistable, nestable, and
composed from ordinary capabilities/workflows. They MUST NOT contain a hidden
reasoning loop or bypass capability policy. Synthesis, validation, observation,
verification, and recovery MAY themselves be represented as workflows.

The kernel SHOULD use reflection to retrieve relevant capabilities,
workflows, skills, runtimes, dependencies, permissions, and project
affordances rather than injecting the complete global inventory into every
model turn.

The complete target model, lifecycle, validation tiers, promotion scopes, and
current alignment boundary are documented in `docs/ARCHITECTURE.md`.

# 172. Evidence/Research Fabric

Athena MUST provide a durable Evidence/Research Fabric distinct from ordinary
Memory, Skills, Capabilities, and Workflows. It MUST persist versioned source
records, content hashes or immutable snapshot references, bounded evidence
objects with exact supporting excerpts and locators, evidence-to-claim links,
corroboration/contradiction relations, and research gaps.

Source authority classification MUST NOT be treated as permission. Any future
external acquisition route MUST enforce an explicit source policy before
fetching, including domain allowlisting and private/local-network controls.
The current `research` capability provides durable record/list/search/verify
operations over captured sources; it MUST NOT be described as a crawler or
internet research agent until acquisition, indexing, and citation verification
are implemented through the canonical capability/policy/execution path.

Research planning MUST be represented by ordinary Tasks and declarative
Workflows. Archivist-style retrieval or critique MUST NOT create a second
reasoning authority or bypass Athena's AgentKernel, PolicyEngine,
ExecutionManager, ArtifactStore, or event ledger. Research evidence MAY guide
Machinist-style generated machinery, and generated machinery MAY create
structured observations, but both directions MUST retain provenance and
inherited authority.
