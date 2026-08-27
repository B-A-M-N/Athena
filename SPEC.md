



:::writing{variant="document" id="43827" title="Athena — Unified Lightweight Autonomous Agent Runtime Specification"}
# Athena

## Unified Lightweight Autonomous Agent Runtime

**Status:** Initial Architecture Specification  
**Target:** Linux, macOS, Windows; headless-first  
**Primary Interface:** CLI/TUI  
**Secondary Interfaces:** Python API, HTTP/SSE/WebSocket API, ACP, optional messaging gateway  
**Design ancestry:** Hermes Agent + Open Interpreter Classic  
**Target production source size:** ~45,000–70,000 LOC excluding tests and bundled skills  
**Core-kernel target:** ~15,000–25,000 LOC  
**Primary objective:** Preserve the highest-value capabilities of Hermes Agent and Open Interpreter Classic while eliminating duplicated agent loops, overlapping execution systems, UI sprawl, provider-specific branching, and unnecessary framework complexity.

---

# 1. Executive Summary

Athena is not intended to be a fork-merger of Hermes Agent and Open Interpreter.

It is a new agent architecture built around the strongest ideas from each:

**From Hermes Agent:**

- persistent sessions
- memory
- reusable/self-improving skills
- subagent delegation
- model/provider abstraction
- MCP integration
- context management
- scheduled autonomous tasks
- remote operation
- gateway interfaces
- agent-oriented lifecycle management

**From Open Interpreter Classic:**

- universal code execution
- persistent language runtimes
- direct shell interaction
- computer/desktop interaction
- simple execution semantics
- local-first operation
- explicit execution approvals
- REPL-like statefulness

Hermes currently routes CLI, gateway, ACP, API, and other entry points into an `AIAgent` that owns prompt construction, provider resolution, tool execution, compression, retries, fallback, cancellation, and persistence. Its documented tool system has grown to 70+ registered tools across roughly 28 toolsets, along with multiple execution backends. citeturn467248view0turn467248view1

Classic Open Interpreter takes almost the opposite approach: it primarily gives the model an `execute` capability accepting a language and code, supports persistent REPL-style runtimes for several languages, exposes direct shell execution and optional computer control, and normally asks for approval before generated code executes. citeturn750356view0

Athena should combine those philosophies as:

```text
Agent intelligence
        +
Durable state
        +
Reusable knowledge
        +
Universal execution
        +
External capabilities
```

with exactly **one authoritative agent loop**.

---

# 2. Product Thesis

Athena should answer the question:

> How small can a genuinely capable autonomous general-purpose agent runtime be without sacrificing the capabilities that make systems such as Hermes useful?

The project must optimize for:

1. **Capability density**
2. **Architectural clarity**
3. **Local control**
4. **Model independence**
5. **Execution power**
6. **Observability**
7. **Recoverability**
8. **Extensibility without core growth**
9. **Reasonable security defaults**
10. **Long-term maintainability**

Athena should not attempt to win by having the most tools, providers, adapters, configuration options, or UI implementations.

It should win by providing a small number of stable abstractions capable of expressing those features externally.

---

# 3. Architectural Principle

The central architecture is:

```text
                         ATHENA
┌───────────────────────────────────────────────────────────────┐
│                                                               │
│                        INTERFACES                             │
│       CLI/TUI       API       ACP       Gateway              │
│                         │                                     │
├─────────────────────────▼─────────────────────────────────────┤
│                                                               │
│                      AGENT KERNEL                             │
│                                                               │
│  Session ──► Context ──► Model ──► Decision ──► Dispatch     │
│     ▲                                         │               │
│     │                                         ▼               │
│  Memory                                  Capability Bus       │
│                                               │               │
├───────────────────────────────┬───────────────┴───────────────┤
│                               │                               │
│       KNOWLEDGE LAYER         │       ACTION LAYER            │
│                               │                               │
│  Skills                       │  Runtime Executor              │
│  Memory                       │  MCP                           │
│  Project Context              │  Native Tools                 │
│  Search                       │  Computer Control             │
│                               │                               │
├───────────────────────────────┴───────────────────────────────┤
│                                                               │
│                     ORCHESTRATION                             │
│                                                               │
│       Delegation        Scheduler        Task Runtime         │
│                                                               │
└───────────────────────────────────────────────────────────────┘
```

The critical invariant is:

> **There is only one component allowed to decide what the agent does next: the Athena Agent Kernel.**

Execution engines execute.

Providers infer.

Skills provide knowledge.

MCP provides external capabilities.

Memory stores knowledge.

Schedulers initiate tasks.

Interfaces transport messages.

None of those components gets its own competing agent loop.

Athena's capability surface is a programmable **Affordance Fabric**, not a
closed list of predefined tools. The fabric combines native and external
capabilities with task/project overlays, scratch computation, generated
capabilities, declarative workflows, and reusable skills. When an existing
affordance is insufficient, the AgentKernel may choose to compose an existing
workflow, construct deterministic machinery, acquire a dependency through
policy, or preserve procedural knowledge. These are all strategies inside the
same Task and event model; none creates a second reasoning authority.

The detailed developer architecture and its explicit implemented-versus-target
boundary live in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

# 4. What Athena Takes From Hermes

Hermes currently provides a broad platform including persistent memory, skill creation and refinement, full-text searchable sessions, scheduled automation, parallel delegation, multiple execution backends, messaging gateways, plugins, MCP, ACP and provider abstraction. citeturn467248view2turn467248view0

Athena should preserve the behaviors that produce leverage while replacing much of the implementation.

## 4.1 Hermes — KEEP

### Agent/tool loop semantics

Keep the fundamental cycle:

```text
user/task
   ↓
context construction
   ↓
model inference
   ↓
tool calls?
 ┌─┴─┐
yes  no
 │    │
execute
 │    │
result│
 └─► model
      │
    answer
```

This remains Athena's primary cognitive execution model.

### Persistent sessions

Keep:

- session IDs
- durable transcripts
- task IDs
- metadata
- usage statistics
- resumability
- parent/child lineage
- searchable history

Hermes already uses SQLite and FTS5 for persistent session storage and lineage. citeturn467248view0

Athena should retain the concept but implement it behind a narrow `SessionStore` interface.

### Context management

Keep:

- token-budget awareness
- conversation compaction
- preservation of recent turns
- selective retrieval of historical context
- stable system instructions
- project context
- memory injection

Do not preserve Hermes' exact implementation.

### Skills

Keep the concept strongly.

Athena skills are reusable procedural knowledge:

```text
SKILL.md
metadata
resources/
scripts/
tests/
```

Skills can teach Athena:

- how a tool works
- how a workflow should be performed
- project-specific procedures
- debugging methods
- deployment processes
- user-defined operations

Hermes explicitly treats skill creation and improvement as part of its learning loop. citeturn467248view2

Athena should retain this capability.

### Memory

Keep:

- durable user/project facts
- agent-learned lessons
- retrieval
- explicit memory writes
- provenance
- confidence
- expiration
- correction
- deletion

Hermes now distinguishes memory providers from context engines, which is a useful architectural separation Athena should retain conceptually. citeturn745892search1turn745892search9

### Delegation

Keep isolated subagents.

Hermes already supports fresh-context delegation and parallel tasks. citeturn745892search2

Athena should retain delegation but simplify the implementation substantially.

### MCP

Keep full MCP client capability.

MCP tools should appear through Athena's generic capability registry rather than becoming special cases inside the agent.

Hermes currently dynamically discovers MCP and plugin tools after core-tool discovery. citeturn745892search3

### Model independence

Keep:

- OpenAI-compatible endpoints
- Anthropic
- local endpoints
- provider plugins
- configurable base URLs
- model aliases
- model-specific capability metadata

### Scheduling

Keep scheduled autonomous jobs.

Hermes treats cron jobs as agent tasks rather than ordinary shell commands. citeturn467248view0

Athena should do the same.

### Remote interfaces

Keep the ability to invoke Athena from:

- API
- ACP
- messaging
- remote CLI
- automation

But move these outside the core.

### Cancellation and interruption

Keep:

- Ctrl-C cancellation
- model-call cancellation
- tool cancellation where possible
- user redirect/interruption
- task termination

Hermes explicitly treats execution as interruptible. citeturn467248view0

---

# 5. Hermes — MODIFY

## 5.1 Replace the large AIAgent responsibility surface

Hermes' documented `AIAgent` currently owns prompt assembly, provider/API-mode selection, model calls, tool execution, conversation history, compression, retries, fallback, budget tracking and memory flushing. citeturn467248view1

Athena must split these responsibilities.

Replace:

```text
AIAgent
 ├ everything
 ├ everything
 └ more everything
```

with:

```text
AgentKernel
ContextManager
ModelRouter
CapabilityRegistry
SessionStore
MemoryStore
ExecutionManager
TaskManager
```

The kernel coordinates them but does not implement them.

## 5.2 Tool system

Hermes' large tool catalog should not be ported directly.

Athena should reduce first-party capabilities to approximately:

```text
execute
filesystem
search
fetch
computer
delegate
memory
skill
schedule
mcp
```

Some of these may expose multiple operations internally.

For example:

```text
filesystem(
    operation="read|write|patch|list|stat|move|delete"
)
```

instead of:

```text
read_file
write_file
patch_file
delete_file
move_file
list_files
...
```

This reduces prompt-token cost, dispatch code, documentation and maintenance.

## 5.3 Provider architecture

Do not preserve giant provider decision trees.

Normalize every model backend to:

```python
class ModelProvider:
    async def complete(request: ModelRequest) -> ModelResponse:
        ...
```

Provider-specific message conversion belongs inside adapters.

The agent kernel sees one representation.

## 5.4 Context compression

Replace tightly coupled compression with:

```python
class ContextStrategy:
    async def build(
        session,
        budget,
        task
    ) -> ModelContext:
        ...
```

Implementations may include:

```text
sliding
summary
retrieval
hierarchical
external
```

The strategy owns compression.

The kernel does not.

## 5.5 Skills

Remove autonomous unrestricted mutation.

Use lifecycle states:

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

Each change stores:

```text
source
reason
session_id
diff
timestamp
confidence
validator
```

Self-improvement should be auditable and reversible.

## 5.6 Memory

Separate:

```text
raw transcript
≠
working context
≠
long-term memory
≠
skill knowledge
```

These should never collapse into one storage abstraction.

## 5.7 Delegation

Hermes delegation should be modified into explicit `TaskSpec` execution.

Instead of:

```python
delegate_task(goal, context)
```

Athena should support:

```python
TaskSpec(
    objective,
    acceptance_criteria,
    context_refs,
    capabilities,
    model_policy,
    resource_budget,
    write_scope,
    timeout,
)
```

Child agents return a structured `TaskResult`.

## 5.8 Gateway

Hermes currently supports a large multi-platform gateway surface. citeturn467248view0

Athena should not place platform integrations in the main repository.

Define:

```python
class ChannelAdapter:
    receive()
    send()
    edit()
    upload()
```

Telegram, Discord, Slack, Signal and others become separate packages.

---

# 6. Hermes — DISCARD

Athena should deliberately discard:

### Tool proliferation

Do not replicate dozens of narrowly differentiated first-party tools.

### Toolsets

Avoid huge manually maintained capability combinations.

Use permissions/capabilities instead.

### Provider-specific logic in the agent loop

Completely remove it.

### Multiple terminal implementations inside the agent

Execution environments belong behind `ExecutionBackend`.

### Messaging-platform implementations in core

External adapters only.

### Giant configuration trees

Prefer sane defaults and composable profiles.

### UI-specific state

The kernel must not know whether a request originated from:

```text
CLI
Desktop
Telegram
API
ACP
```

### Trajectory-generation logic in core

Emit generic structured events instead.

External tooling can convert those events into training trajectories.

### Specialized service integrations as native code

Most should be:

```text
MCP
plugin
skill
script
```

### Self-improvement logic embedded in the turn loop

Move it into post-turn/event consumers.

### Import-time magical registration

Prefer explicit registration.

### Monolithic synchronous orchestration

Athena should be async-first.

---

# 7. What Athena Takes From Open Interpreter Classic

Classic Open Interpreter's defining behavior is much simpler: the model gets an execution mechanism capable of running code and shell commands locally; several languages use persistent sessions; commands are normally user-approved; and its Python implementation can expose local models and programmatic chat. citeturn750356view0

That simplicity is valuable.

---

# 8. Open Interpreter Classic — KEEP

## 8.1 Universal execute primitive

This is the single most important OI concept.

Athena should expose:

```python
execute(
    language="python",
    code="...",
    session="default"
)
```

This capability replaces enormous numbers of bespoke tools.

Examples:

```text
Python
Bash
PowerShell
JavaScript
```

Optional adapters:

```text
Ruby
R
Java
AppleScript
SQL
```

Classic OI supports persistent sessions for several languages and fresh execution for others. citeturn750356view0

Athena should preserve that distinction.

## 8.2 Persistent REPL execution

Python and shell operations should optionally share state.

Example:

```text
execute #1:
df = pandas.read_csv(...)

execute #2:
print(df.describe())
```

without reconstructing execution state unnecessarily.

## 8.3 Direct shell escape

The user should be able to execute commands without involving inference:

```text
!git status
```

Optionally:

```text
!!git status
```

could execute without injecting output into model context.

## 8.4 Execution approval

Retain explicit execution approval as a first-class concept.

Classic OI normally prompts before executing generated code. citeturn750356view0

Athena should improve this into policy-based approval rather than one global `auto_run` switch.

## 8.5 Computer interaction

Retain:

```text
screen observation
keyboard
mouse
window/application interaction
browser interaction
```

but place it behind a dedicated runtime interface.

## 8.6 Local-first semantics

Commands execute where Athena is running unless another backend is explicitly chosen.

No forced cloud sandbox.

No required hosted service.

## 8.7 Streaming execution

Preserve incremental stdout/stderr/event streaming.

---

# 9. Open Interpreter Classic — MODIFY

## 9.1 Remove its agent loop

Athena must not run:

```text
Athena Agent
   ↓
Open Interpreter Agent
   ↓
LLM
```

Instead:

```text
Athena Agent
   ↓
ExecutionManager
   ↓
OI-derived runtime concepts
```

OI becomes infrastructure.

## 9.2 Replace message-driven execution internals

Execution should use explicit typed requests.

```python
ExecutionRequest(
    language="python",
    source="...",
    cwd="/workspace",
    env={},
    timeout=30,
    session_id="...",
)
```

Return:

```python
ExecutionResult(
    exit_code=0,
    stdout="...",
    stderr="",
    artifacts=[],
    duration_ms=...,
)
```

## 9.3 Replace LiteLLM coupling

Model-provider logic belongs entirely in Athena's model layer.

The execution runtime should have **zero awareness of LLM providers**.

## 9.4 Replace OI configuration profiles

Athena has one unified config/profile model.

Do not preserve OI-specific profile semantics.

## 9.5 Replace simplistic auto-run safety

Athena should classify actions.

Example:

```text
READ
WRITE
EXECUTE
NETWORK
INSTALL
PROCESS
PRIVILEGED
DESTRUCTIVE
EXTERNAL_SIDE_EFFECT
SECRET_ACCESS
```

Policy decisions depend on action class and execution scope.

## 9.6 Computer control

Do not make desktop automation another autonomous agent.

Computer control must expose deterministic primitives to the Athena kernel.

---

# 10. Open Interpreter Classic — DISCARD

Do not retain:

- its independent conversation engine
- its model-selection system
- LiteLLM as an architectural requirement
- its system prompt
- its duplicate session/history system
- its agent-specific command parser
- its provider configuration
- its agent-specific UI state
- duplicated context handling
- duplicated approvals
- agent-specific browser reasoning loops
- deprecated OS-mode architecture
- execution logic coupled directly to LLM response formatting

Athena wants **OI's execution philosophy**, not another OI instance hiding inside Athena.

---

# 11. Athena Core Architecture

## 11.1 AgentKernel

The AgentKernel should be deliberately small.

Responsibilities:

```text
receive task
resolve session
request context
request inference
interpret response
dispatch capabilities
record events
repeat until terminal condition
return result
```

Not responsibilities:

```text
SQL
provider authentication
subprocess implementation
MCP transport
memory retrieval algorithms
context summarization algorithms
platform adapters
scheduler timing
desktop automation implementation
```

Proposed API:

```python
class AgentKernel:
    async def run(
        self,
        request: AgentRequest
    ) -> AgentResult:
        ...
```

---

# 12. Canonical Internal Message Model

All providers normalize into one internal event representation.

```python
Message:
    id
    role
    content[]
    timestamp
    metadata
```

Content blocks:

```text
Text
Reasoning
Image
Audio
FileRef
ToolCall
ToolResult
Artifact
```

Do not make OpenAI's current JSON schema the permanent internal architecture.

Provider adapters translate between Athena's schema and provider schemas.

---

# 13. Event Architecture

Every significant activity becomes an event.

Example:

```text
TaskStarted
ContextBuilt
ModelRequestStarted
ModelToken
ModelResponseReceived
CapabilityRequested
ApprovalRequested
CapabilityStarted
StdoutChunk
CapabilityCompleted
MemoryWritten
SkillLoaded
SubtaskStarted
SubtaskCompleted
TaskCompleted
TaskFailed
```

Interfaces subscribe to events.

This means:

```text
CLI
Web UI
ACP
logging
telemetry
trajectory exporter
gateway
```

all observe the same system without adding special cases to the kernel.

---

# 14. Capability Bus

All things Athena can do should resolve through:

```python
CapabilityRegistry
```

A capability has:

```python
Capability:
    name
    description
    schema
    permissions
    executor
    availability
```

Examples:

```text
execute
filesystem
search
fetch
computer
memory
skill
delegate
schedule
```

MCP tools are dynamically registered into this same registry.

The kernel should not care whether a capability originates from:

```text
native Athena
MCP
plugin
project extension
remote worker
```

---

# 15. ExecutionManager

This is Athena's OI-derived subsystem.

```text
ExecutionManager
    │
    ├── LocalBackend
    ├── SSHBackend
    ├── ContainerBackend
    ├── SandboxBackend
    └── RemoteBackend
           │
           ▼
        Runtime
```

Runtime interface:

```python
class Runtime:
    async def start(...)
    async def execute(...)
    async def interrupt(...)
    async def reset(...)
    async def close(...)
```

Initial runtimes:

```text
bash
python
javascript
powershell
```

Optional runtimes can be plugins.

---

# 16. Runtime Sessions

A runtime session stores:

```text
runtime_id
task_id
backend
language
cwd
environment
process
created_at
last_used_at
policy_scope
```

A runtime may be:

```text
persistent
ephemeral
```

The agent chooses based on task requirements and policy.

---

# 17. Filesystem Capability

Filesystem operations should be explicit rather than always performed through shell.

Why?

Because Athena can then:

- audit mutations
- calculate diffs
- request targeted approval
- support rollback
- enforce path scopes
- emit structured events

Operations:

```text
read
write
patch
mkdir
list
stat
move
copy
delete
```

All writes produce mutation records.

---

# 18. Mutation Ledger

Every filesystem mutation should optionally record:

```text
mutation_id
task_id
timestamp
path
operation
old_hash
new_hash
diff
actor
approval
```

For text files Athena should preserve reversible patches where practical.

This allows:

```text
athena undo
athena diff
athena mutations
```

and gives autonomous operation meaningful recoverability.

---

# 19. ContextManager

The ContextManager owns token selection.

Inputs:

```text
current request
recent messages
system policy
project instructions
relevant memory
skills
retrieved session fragments
tool definitions
model capabilities
token budget
```

Output:

```text
ModelContext
```

The kernel never manually manipulates context.

---

# 20. Context Priority

Use ordered priority bands:

```text
P0  Security/policy
P1  User task/instructions
P2  Project instructions
P3  Required tool definitions
P4  Current task state
P5  Recent conversation
P6  Retrieved memory
P7  Relevant skills
P8  Historical context
```

Lower-priority layers are compressed or removed first.

---

# 21. Context Compression

Implement compression as a replaceable strategy.

Default:

```text
recent turns retained verbatim

older relevant turns retrieved

older irrelevant turns summarized

tool outputs aggressively compacted

large artifacts stored externally and referenced
```

Never summarize:

```text
unresolved constraints
explicit user requirements
active acceptance criteria
security decisions
pending mutations
```

---

# 22. Artifact Store

Large outputs should not remain permanently inline in conversation context.

Store:

```text
command output
web content
documents
images
generated files
large tool responses
```

as artifacts.

Messages contain references:

```text
artifact://task/.../...
```

Context retrieves only required slices.

This is essential for keeping long-running agents efficient.

---

# 23. Memory Architecture

Athena memory should use three layers.

## Working Memory

Task-scoped volatile state.

## Episodic Memory

Session/task history.

## Semantic Memory

Durable learned facts and lessons.

Schema:

```text
memory_id
scope
type
content
source
confidence
created_at
updated_at
expires_at
tags
embedding_optional
```

Scopes:

```text
user
project
workspace
global
```

---

# 24. Memory Rules

Athena must not silently treat model speculation as fact.

Memory writes require:

```text
source
confidence
reason
```

Conflicting facts should coexist until resolved.

Users must be able to:

```text
athena memory search
athena memory inspect
athena memory forget
athena memory export
```

---

# 25. Skills Architecture

Skills are human-readable directories.

```text
skill-name/
├── SKILL.md
├── manifest.yaml
├── resources/
├── scripts/
└── tests/
```

Manifest:

```yaml
name: cuda-debugging
version: 1
scope: project
permissions:
  - execute
tags:
  - cuda
  - debugging
```

Skill loading should be retrieval-based.

Do not dump hundreds of complete skills into the prompt.

---

# 26. Skill Self-Improvement

After sufficiently meaningful tasks, Athena may generate:

```text
SkillCandidate
```

It should not immediately overwrite an existing skill.

Flow:

```text
experience
    ↓
candidate
    ↓
diff
    ↓
validation
    ↓
promotion
```

All autonomous modifications remain reversible.

---

# 27. ModelRouter

Provider and model selection should be policy-driven.

```python
ModelRequest:
    purpose
    required_capabilities
    context_tokens
    latency_preference
    cost_budget
    reasoning_requirement
    tool_requirement
```

Model metadata:

```text
context_limit
tool_calling
vision
audio
reasoning
streaming
structured_output
cost
latency
provider
availability
```

This enables intelligent routing without provider logic infecting the agent loop.

---

# 28. Provider Interface

```python
class ModelProvider:
    async def models() -> list[ModelInfo]
    async def complete(request) -> ModelResponse
    async def cancel(request_id)
```

Initially support:

```text
OpenAI-compatible
Anthropic
```

Everything else should preferably use OpenAI-compatible endpoints or provider plugins unless native behavior is materially valuable.

---

# 29. Model Roles

Athena should support optional roles:

```text
primary
fast
reasoner
vision
summarizer
embedding
reviewer
```

But roles are policy aliases.

They are not separate hardcoded agent implementations.

---

# 30. Delegation Architecture

A subagent is another AgentKernel invocation with isolation.

```text
Parent Task
    │
    ├── Child A
    ├── Child B
    └── Child C
```

Each child receives:

```text
objective
acceptance criteria
bounded context
capabilities
workspace scope
model policy
token budget
time budget
```

---

# 31. TaskSpec

```python
TaskSpec:
    id
    objective
    acceptance_criteria[]
    context_refs[]
    capabilities[]
    workspace
    write_scope[]
    model_policy
    token_budget
    time_budget
    parent_id
```

Output:

```python
TaskResult:
    status
    summary
    evidence[]
    artifacts[]
    mutations[]
    unresolved[]
    usage
```

This is much stronger than arbitrary natural-language subagent output.

---

# 32. Delegation Rules

Default maximum depth:

```text
2
```

Configurable but bounded.

Concurrency controlled globally:

```text
max_agents
max_model_calls
max_execution_jobs
```

Children may not automatically inherit:

```text
all secrets
all memory
all tools
all writable paths
```

They receive capability-scoped access.

---

# 33. Parallelism

Athena should support:

```text
parallel independent subtasks
serial dependent subtasks
map/reduce patterns
reviewer/implementer patterns
```

But not create an elaborate distributed-agent framework in v1.

Keep orchestration intentionally primitive.

---

# 34. Scheduler

Scheduled tasks use exactly the same `TaskSpec`.

```text
Scheduler
    ↓
TaskSpec
    ↓
AgentKernel
```

No second automation engine.

Supported triggers:

```text
once
cron
interval
```

Later:

```text
filesystem event
webhook
condition
message
```

---

# 35. Scheduler Persistence

Suggested tables:

```text
jobs
job_runs
job_outputs
```

Each run is an ordinary Athena task and therefore gains:

- logs
- memory
- artifacts
- policies
- model routing
- mutations
- observability

automatically.

---

# 36. Computer Control

Expose computer control through:

```python
computer.observe()
computer.click()
computer.type()
computer.key()
computer.scroll()
computer.move()
computer.wait()
```

Higher-level browser interaction should preferentially use structured browser APIs where available.

Visual computer control should be fallback/general-purpose infrastructure.

---

# 37. Safety Architecture

Athena should not choose between:

```text
always ask
```

and:

```text
run everything
```

Use a policy engine.

Example default:

| Action | Default |
|---|---|
| Read files | Allow |
| Search files | Allow |
| Execute read-only command | Allow |
| Modify project file | Ask/Policy |
| Install package | Ask |
| Network request | Allow/Policy |
| Access credential | Ask |
| Delete file | Ask |
| Kill process | Ask |
| Privileged command | Ask |
| Send external message | Ask |
| Publish/upload | Ask |
| Financial action | Deny/Ask |
| Destructive recursive command | Deny |

Policies may be changed by profile.

---

# 38. Permission Scope

Permissions should support:

```text
once
task
session
project
always
deny
```

and resources:

```text
filesystem path
host
command class
MCP server
credential
application
```

Example:

```text
allow write /home/user/project/**
for current task
```

instead of:

```text
allow all writes forever
```

---

# 39. Secrets

Secrets should live in a credential store.

Tools receive handles where practical:

```text
credential://github/default
```

rather than secret values being injected into prompts.

Providers receive credentials directly through adapters.

Subagents receive only specifically authorized credentials.

---

# 40. Networking

Track outbound destinations where feasible.

Policy can define:

```text
allow github.com
allow pypi.org
deny unknown
```

for sensitive execution profiles.

---

# 41. Plugins

Athena plugins must extend stable interfaces.

Supported extension categories:

```text
provider
runtime
capability
memory
context
channel
event-handler
```

A plugin must not monkey-patch AgentKernel internals.

---

# 42. Plugin Manifest

```yaml
name: athena-example
version: 1.0
api: 1

provides:
  - capability

permissions:
  - network
```

Core API versioning is mandatory.

---

# 43. MCP

MCP should require almost no Athena-specific abstraction.

```text
MCP server
    ↓
MCP client
    ↓
Capability adapter
    ↓
CapabilityRegistry
```

The model sees MCP capabilities just like native capabilities.

---

# 44. Interfaces

Athena interfaces should all connect through one application API.

```text
                    AthenaService
                   /     |      \
                  /      |       \
               CLI      ACP      API
                         |
                       IDE
```

A channel cannot directly instantiate alternative agent logic.

---

# 45. CLI

Primary command:

```text
athena
```

Core commands:

```text
athena chat
athena run
athena resume
athena sessions
athena tasks
athena models
athena skills
athena memory
athena jobs
athena mcp
athena config
athena doctor
athena serve
athena acp
```

Interactive shortcuts:

```text
/model
/session
/context
/tasks
/memory
/skills
/permissions
/undo
/compact
/help

!command
!!command
```

---

# 46. TUI

The TUI should remain deliberately compact.

Panels:

```text
conversation

task/subagent activity

tool/execution stream

optional context/status pane
```

No separate frontend application should be required for full functionality.

---

# 47. API

Recommended API surface:

```text
POST /v1/tasks
GET  /v1/tasks/{id}
POST /v1/tasks/{id}/input
POST /v1/tasks/{id}/cancel

GET  /v1/sessions
GET  /v1/sessions/{id}

GET  /v1/events

GET  /v1/models
GET  /v1/capabilities
```

Streaming:

```text
SSE
```

or:

```text
WebSocket
```

using the same internal event objects.

---

# 48. OpenAI-Compatible Agent Endpoint

Optional:

```text
POST /v1/chat/completions
```

for external software wishing to treat Athena as an inference/agent service.

Important distinction:

Athena is not merely forwarding model calls.

It may execute full agent behavior.

Expose explicit configuration determining whether an endpoint is:

```text
raw inference
agent inference
```

---

# 49. ACP

ACP should be an adapter around `AthenaService`.

Do not introduce ACP-specific agent behavior.

The current Hermes architecture also treats ACP as an entry point into the same agent system, which is the correct conceptual direction. citeturn467248view0

---

# 50. Gateway

Messaging should be a separate package:

```text
athena-gateway
```

Adapter examples:

```text
athena-channel-telegram
athena-channel-discord
athena-channel-slack
```

Core Athena exposes:

```python
submit_message(...)
subscribe_events(...)
```

Nothing more platform-specific is required.

---

# 51. Persistence

Default storage:

```text
SQLite
```

Suggested schema:

```text
sessions
messages
tasks
task_events
artifacts
memories
skills
skill_versions
jobs
job_runs
runtime_sessions
mutations
approvals
```

SQLite gives Athena a single-file durable local state model without requiring external infrastructure.

---

# 52. Search

Start with:

```text
SQLite FTS5
```

for:

```text
sessions
messages
memory
skills
```

Embedding/vector search should be optional.

Do not require an embedding model for basic memory functionality.

---

# 53. Workspace Model

Every task receives a workspace.

```python
Workspace:
    root
    readable_paths
    writable_paths
    temp_path
    runtime_backend
```

This gives file and execution policy a common boundary.

---

# 54. Observability

Observability should be native rather than bolted on.

Every task records:

```text
duration
model calls
tokens
tool calls
tool duration
execution failures
retries
subtasks
mutations
context size
compression
cost if available
```

CLI:

```text
athena inspect TASK_ID
```

should reconstruct exactly what happened.

---

# 55. Structured Logging

Use structured JSON-compatible events internally.

Human rendering happens at the interface.

Never parse pretty terminal output to reconstruct agent behavior.

---

# 56. Error Model

Use typed errors:

```text
ProviderError
AuthenticationError
RateLimitError
ContextOverflow
CapabilityError
ExecutionError
PolicyDenied
Timeout
Cancelled
TaskFailure
```

Retry policy depends on error type.

Do not blanket-retry arbitrary failures.

---

# 57. Retry Strategy

Retries belong in the layer that understands the error.

Examples:

```text
HTTP timeout
→ ProviderAdapter

temporary MCP failure
→ MCP adapter

Python syntax error generated by model
→ AgentKernel receives result and decides

policy denial
→ never automatic retry
```

---

# 58. Fallback Models

Fallback must be explicit policy.

Example:

```yaml
models:
  primary:
    - provider/model-a
    - provider/model-b
```

Never silently move from:

```text
free/local
```

to:

```text
expensive hosted
```

unless configured.

---

# 59. Configuration

Primary config:

```text
~/.config/athena/config.toml
```

Project config:

```text
.athena/config.toml
```

Project instructions:

```text
AGENTS.md
```

Skills:

```text
.agents/skills/
~/.agents/skills/
```

Using portable conventions where practical is preferable to inventing Athena-specific formats. Current Open Interpreter likewise explicitly emphasizes shared `AGENTS.md`, `.agents/skills`, MCP and ACP portability. citeturn467248view4

---

# 60. Configuration Philosophy

Do not expose internal implementation details unnecessarily.

Bad:

```yaml
thread_pool_size: 17
summary_keep_turns_left: 6
gateway_tool_dispatch_mode: foo
```

Good:

```yaml
autonomy: supervised
max_parallel_tasks: 4
context_strategy: default
execution_backend: local
```

---

# 61. Profiles

Profiles define policy bundles.

Example:

```text
safe
coding
research
autonomous
offline
```

A profile can specify:

```text
model policy
permissions
runtime
memory
network policy
context strategy
```

Profiles must not duplicate entire configuration files.

---

# 62. Offline Mode

Athena should have a genuine offline mode:

```text
local model endpoint
local execution
local memory
local skills
network denied
telemetry disabled
```

No external network request should occur unexpectedly.

---

# 63. Multimodality

The internal message model should support from day one:

```text
text
image
audio
file
```

even if initial provider adapters do not support every modality.

This avoids redesigning the message schema later.

---

# 64. Speech

Speech should be plugins/capabilities:

```text
STT
TTS
```

not agent-core responsibilities.

---

# 65. Browser Interaction

Use two strategies:

```text
structured browser automation
visual computer interaction
```

Prefer structured interaction.

Fallback to visual interaction when structure is unavailable.

---

# 66. Project Layout

Recommended repository:

```text
athena/
├── src/athena/
│   ├── kernel/
│   │   ├── agent.py
│   │   ├── lifecycle.py
│   │   └── events.py
│   │
│   ├── models/
│   │   ├── types.py
│   │   ├── router.py
│   │   └── providers/
│   │
│   ├── context/
│   │   ├── manager.py
│   │   ├── strategies/
│   │   └── artifacts.py
│   │
│   ├── capabilities/
│   │   ├── registry.py
│   │   ├── execute.py
│   │   ├── filesystem.py
│   │   ├── computer.py
│   │   └── native/
│   │
│   ├── runtime/
│   │   ├── manager.py
│   │   ├── backend.py
│   │   └── languages/
│   │
│   ├── memory/
│   │   ├── store.py
│   │   └── retrieval.py
│   │
│   ├── skills/
│   │   ├── loader.py
│   │   ├── registry.py
│   │   └── curator.py
│   │
│   ├── tasks/
│   │   ├── manager.py
│   │   ├── delegation.py
│   │   └── scheduler.py
│   │
│   ├── mcp/
│   │
│   ├── policy/
│   │   ├── engine.py
│   │   └── approvals.py
│   │
│   ├── storage/
│   │   ├── sqlite.py
│   │   └── migrations/
│   │
│   ├── service/
│   │   └── service.py
│   │
│   ├── interfaces/
│   │   ├── cli/
│   │   ├── api/
│   │   └── acp/
│   │
│   └── plugins/
│
├── tests/
├── docs/
├── examples/
└── pyproject.toml
```

---

# 67. Target LOC Budget

This is a design constraint, not an achievement metric.

Approximate production-code targets:

| Component | Target LOC |
|---|---:|
| Agent kernel/events | 4k–6k |
| Models/providers/router | 5k–8k |
| Context/artifacts | 4k–7k |
| Runtime/execution | 6k–10k |
| Capabilities/filesystem/computer | 5k–8k |
| Memory | 3k–5k |
| Skills | 3k–5k |
| Tasks/delegation/scheduler | 4k–7k |
| MCP/plugins | 3k–5k |
| Policy/approvals | 3k–5k |
| Storage | 3k–5k |
| CLI/TUI/API/ACP | 6k–10k |

Some functionality overlaps, so these ranges should not simply be summed.

Target:

```text
Core/kernel runtime:
15k–25k

Complete useful Athena:
45k–70k

Tests:
15k–30k

Full repository excluding bundled assets/docs:
~65k–100k
```

If production code reaches roughly:

```text
100k+
```

before substantial third-party integrations exist, architecture review should be mandatory.

---

# 68. Explicit Anti-Bloat Rules

Athena should establish hard project rules.

## Rule 1

No file should routinely exceed ~1,000 LOC.

## Rule 2

No provider conditionals in AgentKernel.

## Rule 3

No channel/platform conditionals in AgentKernel.

## Rule 4

No model-specific prompt hacks in generic code unless expressed through model capability metadata.

## Rule 5

No capability gets merged if it could reasonably exist as an MCP server or plugin without degrading the normal user experience.

## Rule 6

No second agent loop.

## Rule 7

No separate persistence systems for individual interfaces.

## Rule 8

No UI becomes authoritative state.

## Rule 9

No self-modification without provenance and rollback.

## Rule 10

No abstraction without at least two meaningful implementations or an immediate architectural reason.

---

# 69. Turn Lifecycle

Canonical lifecycle:

```text
1. Receive AgentRequest

2. Resolve/create Session

3. Create/restore Task

4. Resolve Workspace + Policy

5. ContextManager.build()

6. ModelRouter.select()

7. Provider.complete()

8. Normalize ModelResponse

9. If capability calls:
      validate
      policy check
      request approval if needed
      execute
      persist results
      emit events
      return to step 5

10. If delegation:
      create TaskSpec
      execute child task(s)
      attach TaskResult
      return to step 5

11. If final response:
      persist
      run post-turn hooks
      return AgentResult
```

---

# 70. Post-Turn Processing

Post-turn processes may perform:

```text
memory candidate extraction
skill candidate extraction
session indexing
artifact cleanup
telemetry
task summary generation
```

They must not block normal user interaction unless explicitly configured.

---

# 71. Background Architecture

A small task worker can run:

```text
scheduler
post-turn jobs
gateway processing
background delegation
```

Do not require Redis, Celery or Kubernetes.

Initial implementation:

```text
asyncio + SQLite
```

External queue adapters can arrive later.

---

# 72. Data Sovereignty

Athena should make data movement explicit.

Every capability/provider may expose:

```text
local
remote
hybrid
```

A privacy policy can forbid sending:

```text
file contents
memory
secrets
specific paths
specific artifact classes
```

to remote models.

Provider adapters should receive the already-authorized context rather than arbitrary access to Athena's state.

---

# 73. Provenance

Every retrieved context fragment should retain provenance:

```text
session
memory
skill
file
MCP
web
user
system
```

This allows debugging:

> Why did Athena believe this?

and:

> Where did this instruction come from?

---

# 74. Task Completion

Agents frequently claim completion prematurely.

Athena should support optional acceptance criteria.

Before terminal completion:

```text
TaskSpec.acceptance_criteria
```

may be evaluated.

Possible states:

```text
complete
partial
blocked
failed
cancelled
```

Do not force everything into success/failure.

---

# 75. Verification

For coding/execution tasks Athena should strongly encourage:

```text
perform
    ↓
verify
    ↓
report
```

But verification remains task-aware rather than universally requiring an LLM reviewer.

---

# 76. Optional Reviewer

For high-value work:

```text
worker
   ↓
result
   ↓
reviewer
   ↓
accept / repair
```

Reviewer is simply another bounded `TaskSpec`.

No special reviewer framework is required.

---

# 77. Recovery

After a crash Athena should restore:

```text
sessions
task state
artifact metadata
mutation ledger
scheduler state
```

Processes that cannot survive should be marked:

```text
interrupted
```

rather than falsely appearing successful.

---

# 78. Testing Strategy

Athena should have four primary test layers.

## Unit

Interfaces and pure components.

## Contract

Every provider/runtime/plugin implementation must satisfy common behavioral contracts.

## Integration

Agent → provider fake → capability → persistence.

## End-to-End

Real local task execution.

Examples:

```text
create file
modify project
run Python
delegate analysis
resume session
execute scheduled task
MCP call
cancel command
recover after simulated crash
```

---

# 79. Deterministic Test Provider

Ship a fake model provider capable of scripted responses.

Example:

```yaml
responses:
  - tool_call:
      name: filesystem
      args: ...
  - text: complete
```

This allows testing the entire agent lifecycle without inference cost or nondeterminism.

---

# 80. Runtime Contract Tests

Each runtime must prove:

```text
stdout
stderr
exit status
timeout
interrupt
cwd
environment
persistent state
cleanup
```

before being considered supported.

---

# 81. Security Tests

Explicit tests should cover:

```text
path traversal
symlink escapes
command cancellation
secret leakage
subagent permission inheritance
approval bypass
MCP capability escalation
dangerous deletion
workspace escapes
malicious skill instructions
```

---

# 82. Compatibility Targets

Athena should prioritize open interfaces:

```text
OpenAI-compatible inference
MCP
ACP
AGENTS.md
agentskills-style skill layout
HTTP/SSE
JSON event logs
```

This reduces ecosystem lock-in.

---

# 83. Implementation Language

Python is the most pragmatic initial implementation language.

Reasons include:

```text
AI ecosystem availability
OI/Hermes conceptual ancestry
fast iteration
subprocess/runtime integration
MCP ecosystem
SQLite
asyncio
cross-platform support
```

Performance-sensitive components can later move behind stable interfaces.

Do not begin by rewriting the system in Rust merely to reduce runtime overhead.

Agent systems are predominantly constrained by:

```text
model latency
network latency
tool execution
process execution
```

rather than Python dispatch overhead.

---

# 84. Licensing Strategy

This deserves explicit treatment.

Current Hermes Agent is MIT licensed. citeturn846299search0

The community-maintained Classic Python Open Interpreter repository identified by the current Open Interpreter project is AGPL-3.0. citeturn467248view4turn846299search1

Therefore Athena should choose one of two strategies.

## Preferred: Clean architecture implementation

Study OI behavior and interfaces, but independently implement:

```text
ExecutionManager
persistent language runtime
approval semantics
computer abstraction
```

Do not copy AGPL implementation code.

This permits Athena to choose its own compatible license, subject to the licenses of dependencies actually incorporated.

## Alternative: Direct OI derivative

If Athena directly incorporates AGPL OI implementation code, assume that AGPL obligations may materially constrain distribution and network use of the combined work unless a qualified licensing review establishes otherwise.

For a project intended to remain permissive and broadly embeddable, the first strategy is cleaner.

---

# 85. What Current Open Interpreter Is Not

The present official Open Interpreter repository has moved to a Rust/Codex-derived architecture focused heavily on harness emulation; its own README now points users seeking the original Python architecture to the community-maintained Classic fork. citeturn467248view4

Therefore Athena should **not** use the current Rust Open Interpreter architecture as the blueprint for this design.

The relevant design ancestor is Classic OI's execution philosophy.

---

# 86. Development Phases

## Phase 0 — Contracts

Implement types/interfaces only:

```text
AgentRequest
AgentResult
TaskSpec
ModelProvider
Capability
ExecutionBackend
Runtime
ContextStrategy
SessionStore
Event
```

No feature work before contracts stabilize.

## Phase 1 — Minimum Agent

Implement:

```text
AgentKernel
OpenAI-compatible provider
session storage
context
filesystem
bash
python
CLI
approval
events
```

At this stage Athena should already be useful.

## Phase 2 — Durable Agent

Add:

```text
memory
skills
artifact store
context compaction
mutation ledger
resume/recovery
```

## Phase 3 — Extensibility

Add:

```text
MCP
plugins
Anthropic provider
ACP
HTTP API
```

## Phase 4 — Orchestration

Add:

```text
TaskSpec
delegation
parallel children
scheduler
review workflows
```

## Phase 5 — Computer Interaction

Add:

```text
screen
keyboard
mouse
structured browser
visual fallback
```

## Phase 6 — Remote Operation

Add separate:

```text
gateway
channel adapters
remote workers
```

## Phase 7 — Learning

Add:

```text
memory extraction
skill candidates
skill validation
skill refinement
```

Do this late.

Self-improvement should not be allowed to destabilize the core architecture before the runtime itself is reliable.

---

# 87. V1 Definition

Athena v1 should ship when it can reliably:

1. Converse with hosted and local OpenAI-compatible models.
2. Execute Bash and Python.
3. Maintain persistent runtime sessions.
4. Read/write/patch files.
5. Resume prior conversations.
6. Search sessions.
7. Maintain explicit durable memory.
8. Load reusable skills.
9. Connect to MCP servers.
10. Delegate bounded subagents.
11. Run parallel independent subtasks.
12. Schedule agent tasks.
13. Apply scoped execution approvals.
14. Record a complete event history.
15. Expose CLI/TUI.
16. Expose HTTP streaming API.
17. Expose ACP.
18. Recover cleanly from interrupted tasks.
19. Track filesystem mutations.
20. Run entirely against local inference.

Computer GUI interaction may be v1 or v1.1 depending on implementation quality.

Messaging adapters should not block v1.

---

# 88. Acceptance Criteria

Athena succeeds only if the final system demonstrates all of the following.

### Architectural

```text
one agent loop
one session authority
one model abstraction
one capability registry
one event system
one task abstraction
```

### Capability

It retains most of the practical value associated with:

```text
Hermes:
memory
skills
delegation
MCP
scheduling
remote interfaces
provider flexibility

OI Classic:
code execution
shell execution
persistent REPLs
local system access
computer control
explicit execution approval
```

### Maintainability

A developer should be able to trace:

```text
user request
→ model call
→ capability
→ result
→ next model call
```

without crossing dozens of unrelated modules.

### Extensibility

Adding a new provider, runtime, MCP capability or channel must not require editing AgentKernel.

### Sovereignty

Athena can operate completely locally.

### Recoverability

Tasks, mutations and autonomous changes are inspectable and recoverable.

### Size

The complete production application should preferably remain below approximately:

```text
70,000 LOC
```

excluding:

```text
tests
docs
generated code
third-party code
bundled skills
external plugins
```

This is a guardrail, not an arbitrary code-golf requirement.

---

# 89. Athena's Core Identity

Athena should ultimately be understood as five things:

```text
1. Agent Kernel
   Decides.

2. Context + Knowledge System
   Remembers and understands.

3. Capability Bus
   Describes what can be done.

4. Execution Runtime
   Does it.

5. Task Runtime
   Coordinates work over time.
```

Everything else attaches to those systems.

---

# 90. Final Architecture

```text
                              USER
                                │
          ┌─────────────────────┼────────────────────┐
          │                     │                    │
         CLI                   ACP                  API
          │                     │                    │
          └─────────────────────┼────────────────────┘
                                ▼
                         AthenaService
                                │
                                ▼
                    ┌─────────────────────┐
                    │     AgentKernel     │
                    │                     │
                    │ decide → dispatch   │
                    │   ▲          │      │
                    └───┼──────────┼──────┘
                        │          │
              ┌─────────┘          └────────────┐
              ▼                                 ▼
       ContextManager                    CapabilityRegistry
              │                                 │
      ┌───────┼─────────┐             ┌─────────┼─────────┐
      ▼       ▼         ▼             ▼         ▼         ▼
   Session  Memory    Skills       Execute    MCP     Computer
      │                              │
      │                              ▼
      │                       ExecutionManager
      │                              │
      │                ┌─────────────┼─────────────┐
      │                ▼             ▼             ▼
      │              Local          SSH         Sandbox
      │                │
      │          ┌─────┼─────┐
      │          ▼     ▼     ▼
      │        Bash  Python  JS
      │
      └────────────────────────────────────┐
                                           │
                                           ▼
                                      SQLite State

              AgentKernel
                   │
                   ▼
               TaskManager
              /           \
             ▼             ▼
        Delegation      Scheduler
             │
             ▼
        AgentKernel
       bounded child
```

The architecture deliberately avoids:

```text
Hermes
   +
Open Interpreter
   =
two giant applications
```

and instead becomes:

```text
Hermes' durable-agent concepts
              +
OI Classic's universal execution concepts
              ↓
            Athena
```

---

# 91. Bottom-Line Design Decision

Athena should **not** be described internally as:

> Hermes Lite with Open Interpreter added.

That framing encourages implementation inheritance and eventually reproduces both systems' accumulated complexity.

The better definition is:

> **Athena is a compact, local-first autonomous agent runtime with durable knowledge, structured delegation, universal machine execution, open capability protocols, and a single authoritative reasoning loop.**

Hermes provides much of the model for **what Athena should know how to manage**.

Open Interpreter Classic provides much of the model for **how Athena should act on a computer**.

Athena's contribution is the boundary between those concerns.

That boundary is what makes the smaller system possible.

---

# 92. Evidence and Research Fabric

Athena's durable knowledge model distinguishes:

```text
Memory       useful remembered conclusion
Skill        procedural knowledge
Evidence     why a factual claim is believed
Capability   governed callable operation
Workflow     deterministic composition/procedure
```

The Evidence/Research Fabric MUST store versioned `SourceRecord`s, bounded
`EvidenceObject`s, evidence-to-claim links, contradiction/corroboration links,
and open `ResearchGap`s. A source record SHOULD reference an immutable
ArtifactStore snapshot and content hash. An evidence object MUST retain its
source identity, extracted claim, exact supporting excerpt, locator, and
extraction provenance.

Source classification is a ranking signal, not an authorization decision.
External acquisition MUST pass an explicit pre-fetch source policy, including
domain and private-network controls. Network acquisition, retrieval, indexing,
gap analysis, and citation verification MUST use ordinary Athena capabilities
and workflows under the canonical PolicyEngine and ExecutionManager path.

Archivist-style research planning MUST NOT introduce a second reasoning loop.
The AgentKernel remains the only component that interprets observations,
replans, and decides completion. Research MAY inform generated capability or
workflow construction, and generated machinery MAY produce structured research
observations, but both remain subject to inherited authority and durable
provenance.
:::
