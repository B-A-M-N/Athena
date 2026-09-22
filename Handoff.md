# Handoff — Remaining Work

> Authoritative goal: implement the audit in
> `/home/bamn/.harvardcodex/attachments/f72f7f7e-f7e8-455b-b3cc-5a0a8bfa4454/pasted-text-1.txt`.
> This file records **what is still left**, not completed history.

## Status Vocabulary

- `DONE` — implementation and current executable evidence satisfy the item.
- `OPEN` — no authoritative implementation yet.
- `PARTIAL` — implementation exists; named residual work remains.
- `VERIFY` — code may already satisfy the contract; final requested gate evidence is missing.
- `BLOCKED_IN_SANDBOX` — host capability prevents verification in this checkout only.

## Current Branch State

- Branch: `native-tui-visuals`; dirty integration tree remains uncommitted.
- The prior handoff recorded **2193 passed, 21 skipped, 3 failed** before the
  final Hermes lifecycle-port fix. That was not a certification of the
  current dirty tree: two full-suite failures remain recorded at the known
  host/toolchain boundaries (loopback HTTP fixture and locked MCP 2.x
  transport), and the scenario manifest predates substantial current work.
  The current full suite has not been rerun after the correctness fixes below.
- Current focused evidence: 84 relevant tests pass across model/kernel,
  lock-cancellation, scenario/release, and release-evidence lanes; targeted
  Ruff, MyPy, compileall, `git diff --check`, and architecture lint (0
  violations) pass. VHS was intentionally skipped on request. Required
  `environment_unavailable` scenarios now fail the scenario gate; the
  previously recorded 3 deferred scenarios therefore cannot be treated as a
  green result.
- The source-size audit reports approximately 146,355 Python lines under
  `src/athena/` and 14,188 Rust lines under `native/src/`, excluding tests.
  Those figures, plus the still-broad capability registration surface, mean
  extraction progress is not evidence that capability families or authority
  boundaries have disappeared.
- Native evidence after the X11/input/pointer/resize/text/color seams: locked offline build, check, 71 tests,
  Clippy `-D warnings`, and format check all pass.
- New seams in this pass: bounded `ProjectIndexCache`; canonical keyed-lock
  consolidation for runtime/project state; typed ACP paused lifecycle and
  explicit-input validation; `InferenceBroker` attempt ownership and fallback
  preparation; `WorkflowIdentity`; synthesis operation handlers; framebuffer
  cache ownership; `ReadinessPorts`; `x11/event_loop.rs::WindowSession`;
  backend readiness; reality routing operations; workflow external-effect
  reconciliation; affordance reflection ports; dispatcher retry; and the
  explicit lifecycle port owner boundary; bounded inference retry
  orchestration; workflow bootstrap identity/recovery; reality route
  orchestration; read-only runtime readiness projection; reality route-state
  ownership; reality request
  classification adaptation; workflow step-item transactions; successful
  inference-attempt accounting; synthesis candidate/promotion handlers;
  provider stream consumption and response-receipt normalization; workflow
  continuation completion and nested-run wake-up mechanics; reality
	transaction compensation/recovery/finalization operations; reality branch
	registry/lifecycle; bounded reflection host-inventory projection; execution
	routing registry/selection; pack lifecycle API ports; the single
	kernel reasoning-loop mechanism; projection execution-stream reduction;
  reflection permission projection; native `x11/window_runtime.rs` lifecycle
  coordination; native X11 window/GLX setup ownership; native layout-diagnostics ownership; native frame
  cadence/presentation ownership; native X11 keyboard/input, pointer/selection,
  configure-resize, bounded text-width, and bounded Xft color-cache ownership;
  explicit TaskAPI, candidate, operator-query, and interaction operation-port
  boundaries; the narrower startup acquisition/binding `StartupPorts` boundary;
  execution session lifecycle coordination; workflow step preparation;
  explicit TaskIntake admission ports; explicit provider-construction ports;
  explicit MCP runtime ports;
  explicit watcher resource ports; explicit provider-outcome recovery ports;
  dual-pane terminal/animation lifecycle ownership;
  static SSH backend capability projection;
  deterministic framebuffer Buddy-placement ownership;
  read-only workflow-run query projection; read-only capability search/ranking
  projection; read-only service live-health assembly;
  bounded environment-passport assembly;
  authenticated SSH transport
  mechanics;
  Docker container transport mechanics; bounded environment-file and
  toolchain probes; derived memory embedding-index mechanics; pending
  memory-candidate lifecycle; memory retrieval SQL; self-host operation-port
  and completion-proof mechanics; dual-pane canonical-event projection;
  synthesis target revalidation, candidate construction, and validation/admission
  helpers; validated generated-runtime executor ownership; stateless dual-pane
  frame composition; trusted verification-environment record and validation
  ownership; canonical memory-row and metadata codec ownership; Docker
  persistent worker/session mechanics; authenticated SSH worker/session
  mechanics; explicit SSH profile validation; explicit Fusion composition
  ports; explicit self-host port ownership; bounded execution-result
  collection and execution-stream coordination; explicit execution shutdown
  coordination; explicit startup-recovery reconciliation; explicit self-host
  verification/preflight ownership; retained Glass pixel-layer presentation;
  bounded self-host planner invocation and plan-item preparation; stateless
  dual-pane chassis and control-rail composition;
  explicit durable startup-state acquisition; explicit execution-startup
  graph acquisition; bounded native text-width cache ownership and lifecycle
  evidence; explicit TaskIntake facade use without partial-host lazy
  construction; RealityGate route-state public projections with legacy private
  state aliases removed; canonical risk predicates with duplicate sensitivity
  implementations removed; ExecutionManager routing/cancellation aliases
  removed; static synthesis descriptor/world-pixel ownership; synthesis result
  and candidate-support contracts moved out of the capability module; read-only
  inference-prefix cache observation; conflict-aware memory write coordination;
  read-only memory-record query projection; protocol-stable
  environment-record composition; and container session lifecycle/reattachment
  mechanics; generated overlay lifecycle ownership; named candidate,
  interaction, and synthesis mechanism operations with compatibility-only
  facade wrappers; explicit direct-execution dispatcher/approval/transcript
  ownership through `DirectExecutionPorts`; explicit authority composition
  through `AuthorityComposer`; optional Hermes referee lifecycle ownership
  through `HermesRuntime`; and persisted self-host continuation state
  ownership through `SelfHostContinuationService`; and public-name
  lifecycle/startup resource access through `ServiceLifecycle.ports`,
  `LifecyclePorts`, and `StartupPorts`, including public-name stop teardown
  through `stop_sequence.py`; explicit core-capability wiring resources through
  `CoreCapabilityPorts`; and public-name MCP transport lifecycle through
  `MCPRuntimePorts`; read-only live-health facts through `HealthPorts`.
  The operator backend matrix and task-resource finalizer now consume their
  own explicit read-only/resource ports as well.
- `static-critical`, full Ruff format/check, MyPy, compileall, and
  `git diff --check` are clean. The checkout remains intentionally dirty and
  uncommitted; unrelated work was preserved.

## Remaining Correctness / Audit Verification

| ID | Item | Status | Files / evidence needed | Definition of done |
|---|---|---|---:|---|---|
| A1 | Complex-coding execution-backend probe uses canonical ExecutionManager resolution | DONE | `service/readiness.py`, `execution/manager.py`, readiness/backend parity tests | Canonical backend status is used; unknown, unavailable, and healthy cases are covered. |
| A2 | Canonical task-autonomy policy parity | DONE | `service/readiness.py`, `concurrency/autonomy.py`, autonomy parity tests | One resolver covers supervised/coding/autonomous/offline and invalid persisted values fail closed. |
| A3 | Candidate clone transport preflight | DONE | `shadow/engine.py`, `service/readiness.py`, clone/manifest tests | Actual clone transport is probed and clone/source proof is required; empty state roots fail closed. |
| A4 | Persisted scheduled execution-plan authority | DONE | `scheduler/control.py`, `scheduler/authority.py`, schedule snapshot tests | Stored plans survive intake and classifier changes cannot downgrade an occurrence. |
| A5 | Self-host task-status contract | DONE | `service/self_host.py`, self-host status tests | Enum-based active/resumable/paused/terminal handling covers every `TaskStatus`. |
| A6 | ACP paused lifecycle | DONE | `acp/adapter.py`, ACP lifecycle tests | `FINAL_STATUSES` owns finality; paused events are nonterminal and resumable. |
| A7 | ACP explicit-input validation | DONE | `acp/adapter.py`, ACP validation tests | Explicit invalid autonomy/deadline values raise typed errors before task creation. |
| A8 | `TaskAPI.wait_for(0)` | DONE | `service/task_api.py`, zero-timeout tests | Zero and negative timeouts return immediately; `None` keeps the default. |
| A9 | Bounded keyed state / lock retention | DONE | `concurrency/`, runtime/project/workflow/reality/artifact adoption, retention tests | Churn tests prove ephemeral keyed locks and bounded/releasable project-index cache state. |
| A10 | Broad exception / subprocess policies | DONE | policy JSON files, architecture lint | Current tree has 0 policy violations; new broad catches require inline rationale or hash-pinned policy. |
| A11 | Lock cancellation preserves foreign ownership | DONE | `capabilities/dispatch_mechanisms.py`, `test_lock_cancellation.py` | Cancellation releases only successfully acquired locks and all reservations; two-task resource and ambient-lane contention tests keep the holder's lock held until its owner releases it. |
| A12 | Required scenario/release evidence cannot be deferred as a pass | DONE | `scripts/scenarios`, `scripts/release-check`, `scripts/verify-release-evidence` | Required `environment_unavailable` is nonzero; release-check independently validates every required scenario status and the final releasability predicate requires the scenario gate. |
| A13 | Acceptance verifier result cardinality is fail-closed | DONE | `kernel/termination.py`, `test_termination.py` | Zero, short, excessive, malformed, and verifier-exception outcomes leave required criteria unresolved; only exact boolean result lists can satisfy criteria. |
| A14 | Incomplete model streams cannot complete tasks | DONE | `protocol/models.py`, `kernel/inference_stream.py`, response-stream tests | Successful assembly requires `DONE` with a response; clean empty streams fail as provider errors, partial unterminated streams become unknown/recovery outcomes, and partial output is emitted as diagnostics. |

## Remaining Decomposition

| ID | Object | Status | Current / budget | Remaining work |
|---|---|---|---:|---|
| D1 | `RealityGate` | PARTIAL | 194 / 269 class/module lines | Route record, request predicates, request-to-facts classification, transaction persistence, branch/checkpoint mechanics, concrete route orchestration, transaction compensation/recovery/finalization operations, active/ephemeral branch lifecycle, and durable route-state ownership are extracted. Public branch/transaction projections now replace the former private state aliases; canonical risk predicates live in `request_risk.py` and duplicate implementations were removed from `sensitivity.py`. Remaining work is any further route/transaction narrowing required by the audit. |
| D2 | `ExecutionManager` cancellation reconciliation | PARTIAL | 203 / 249 class/module lines | `CancellationRegistry` now owns execution/session indexes, pending persistence, cancellation reconciliation, and retry state. Late-adoption and direct persistence-retry tests pass; manager-level mutable-state aliases were removed and tests now target the explicit registry owner. Remaining manager work is limited to integrating the registry with the manager's final execution coordination. |
| D3 | `ExecutionManager` module/class | PARTIAL | 385 / 440 class/module lines | Receipts, backend catalog/readiness, runtime readiness projection, cancellation registry, runtime/backend routing registry, execution session lifecycle coordination, bounded streamed-output/result collection, and shutdown coordination are extracted. `ExecutionStreamCoordinator` owns backend/runtime event normalization, session adoption, execution receipts, and stream observability; `ExecutionShutdownCoordinator` owns task/runtime/backend cleanup and live-resource projection; manager-level routing and cancellation aliases resolve directly through their owners, while runtime selection and final execution coordination remain manager-owned. |
| D4 | `lifecycle.py` startup phases | PARTIAL | 347 / 399 class/module lines | `StartupPhaseRunner` owns named initialization, binding, registration, recovery, interface, and worker-start phases; `startup_state.py` owns database readiness and durable store-graph acquisition; `startup_execution.py` owns execution backend/runtime registration and capability-health rehydration. `AuthorityComposer` now owns construction of memory, policy, dispatch, budget, task, and cancellation authorities through `AuthorityCompositionPorts`; `StartupRecovery` owns durable resource/task/execution reconciliation through `StartupRecoveryPorts`; `ServiceLifecycle.ports` exposes the lifecycle boundary, `LifecyclePorts` and `StartupPorts` expose public names with owner-preserving writes, `reasoning_components.py` consumes that explicit port, and `stop_sequence.py` uses the same public teardown boundary. The lifecycle ratchet was lowered after the migration; the port alias/denial contract has focused evidence. |
| D5 | `x11.rs` event/window session | PARTIAL | `x11.rs` 484; runtime 488; input 215; pointer 244; resize 94; setup 196; frame 212 | `WindowSession` owns persistent drag/pending-gesture state and WM fallback timing; layout diagnostics, window/GLX setup, frame cadence, keyboard/XIM translation, pointer/selection/WM gestures, and configure-resize resource updates each have explicit subordinate owners; `window_runtime.rs` retains the event loop and lifecycle authority. Remaining work is only further event/lifecycle narrowing if the original audit requires it. |
| D6 | `render/text.rs` | PARTIAL | 169 / 231 lines; width cache 58; color cache 84 | `FontCatalog` owns Xft face acquisition, metrics, scale reconfiguration, and teardown; `TextWidthCache` owns bounded width entries and clear-on-scale-change behavior with a dedicated lifecycle test; `XftColorCache` owns bounded color allocation/reuse and teardown; `TextRenderer` owns drawing and clipping. Remaining work is any further native text/platform split required by the audit. |
| D7 | `AthenaService` / lifecycle reach-through | PARTIAL | 2,194 / 2,361 class/module lines | Service ports now cover lifecycle, readiness, task intake, task API, self-host, recovery, interaction, candidates, operator query, pack lifecycle, provider construction, MCP runtime, watcher resources, provider-outcome recovery, direct execution calls, Hermes runtime calls, and core-capability wiring. TaskIntake, TaskAPI, CandidateService, OperatorQueryService, OperatorInteractionService, and DirectExecutionService now resolve their application calls through explicit operation allowlists rather than `.owner`/`_svc` escape paths; named mechanism operations now back the facade's compatibility wrappers. `ProviderRuntime` is limited to configuration/credential ports, `MCPRuntime` to public MCP resources/state/secrets/reconnect callbacks through `MCPRuntimePorts`, watcher polling to `WatchPorts`, provider recovery to `ProviderRecoveryPorts`, live health projection to `HealthPorts`, operator readiness to `OperationalMatrixPorts`, and task-resource cleanup to `ResourceFinalizerPorts`. `PackAPI` is an explicit seam; self-host application operations use `self_host_ports.py`, checkout/toolchain validation and preflight records use `self_host_verification.py`, completion-proof mechanics live in `self_host_completion.py`, typed Fusion port construction lives in `fusion_composition.py`, read-only live health assembly lives in `health.py`, startup authority construction lives in `authority_composition.py`, optional Hermes status/preflight/configuration/shutdown lives in `hermes_runtime.py`, lifecycle/startup/reasoning composition now uses public `LifecyclePorts`/`StartupPorts` names without `_svc` call-site reach-through, and `core_capabilities.py` consumes `CoreCapabilityPorts`. `_build_task_spec` now requires the explicitly composed `TaskIntake` facade; partial test hosts attach that port explicitly instead of triggering lazy support-object construction. Remaining service-port work is the larger core composition and compatibility surface outside these migrated mechanisms. |
| D8 | `InferenceBroker` `_invoke` | PARTIAL | function 16 / 16; class 391 / 391 | Cached replay, one-provider-attempt accounting, successful-attempt accounting, fallback candidate/context preparation, bounded retry orchestration, provider stream/receipt normalization, route metadata/replay-boundary projection, and cache-prefix observation are extracted; remaining broker coordination stays broker-owned. |
| D9 | `WorkflowRunStore` | PARTIAL | class 259 / 489 | Receipt persistence, immutable identity, receipt-bound external-effect reconciliation, start/resume identity/recovery bootstrap, stable pre-dispatch step preparation, read-only run projection, nested step-item transactions, continuation completion/wake-up mechanics, and run-level status/output/workspace-baseline persistence are extracted. Remaining workflow transaction coordination stays in the store. |
| D10 | `SynthesisCapability` / synthesis surfaces | PARTIAL | class 28 / 28 | Static model-facing descriptor/schema is extracted into `synthesis_descriptor.py`; result construction and deterministic fixture/dependency merging are neutral support contracts in `synthesis_results.py` and `synthesis_support.py`. Admission routing and candidate/promotion/scratch handlers are extracted into `synthesis_admission.py` and `synthesis_operations.py`; repair/contract candidate construction, target revalidation, validate/admit mechanics, and validated generated-runtime executor ownership now have explicit subordinate helpers. Remaining engine coordination stays engine-owned. |
| D11 | CLI god objects / surfaces | PARTIAL | `OIFrameBuffer` 682 / 682; `DualPaneSurface` 487 / 487 | Framebuffer font/base/PNG cache ownership, deterministic Buddy collision/placement geometry, and bounded animated Buddy terrain/dot-matrix pixels are extracted and tested. `DualPaneEventProjection` owns canonical-event fan-out into projection, mascot, and raw OI stream; `DualPaneFrameComposer` owns stateless bounded line/scene composition; `DualPaneChassisComposer` owns stateless aperture/control-rail geometry; `GlassPresentation` owns retained Kitty pixel-layer placement and image identity cleanup; `DualPaneLifecycle` owns terminal open/close and Glass-only animation invalidation. Dual-pane projection state and remaining frame rendering coordination remain. |
| D12 | Other >600-line ratchets | PARTIAL | `ssh.py` 309 / 309; `SSHBackend` 261 / 261; `container.py` 357 / 312; `ContainerBackend` 312 / 312; `environment.py` 186 / 158; `memory/store.py` 392 / 392; `MemoryStore` 360 / 360; `service/self_host.py` 308 / 268; `SelfHostService` 268 / 268 | `CapabilityFabric` now delegates generated overlay registration/revision/deprecation lifecycle to `overlay_lifecycle.py` in addition to read-only descriptor/availability/provenance/search projections, `CapabilityDispatcher` delegates bounded retry mechanics, lifecycle ports are explicit, `AgentKernel` delegates the single reasoning-loop mechanism, `ProjectionState` delegates execution-stream reduction, `CapabilityReflection` delegates policy/approval, host-inventory, and full environment-passport composition, `SSHBackend` delegates authenticated remote transport/session-supervisor mechanics, session creation, and restart-time identity proof to `ssh_transport.py`, `ssh_session.py`, and `ssh_lifecycle.py` plus its static capability contract to `ssh_capabilities.py`, while `ssh_profile.py` owns operator-profile validation; `ContainerBackend` delegates Docker availability, image identity, command construction, inspection, cleanup, persistent worker/session mechanics, and session creation/reattachment lifecycle to `container_transport.py`, `container_session.py`, and `container_lifecycle.py`, `ProjectEnvironmentFingerprint` delegates bounded file/toolchain probes and protocol-stable record composition to `environment_probe.py` and `environment_record.py`, and `VerificationEnvironment` owns trusted host-selected toolchains, mounts, and record validation in `verification_environment.py`; `MemoryStore` delegates conflict-aware write admission/insertion to `memory/writes.py` plus row/metadata serialization, read-only record queries, derived vector indexing/backfill/search, pending-candidate lifecycle, and lexical/recency SQL to `memory/record_codec.py`, `memory/queries.py`, `memory/embedding_index.py`, `memory/candidate_lifecycle.py`, and `memory/retrieval_store.py`; self-host now delegates bounded planner invocation, completion proof, and persisted mission continuation state transitions to `self_host_planning.py`, `self_host_completion.py`, and `self_host_continuation.py`, while the orchestration class no longer retains an unused service back-reference. Remaining D12 class/service ratchets continue at real ownership boundaries. Never widen budgets; lower after extraction. |

## Tooling / CI / Hygiene

| ID | Item | Status | Remaining work |
|---|---|---|---:|
| T1 | Exact ratchet-headroom tests | DONE | Completed seams have exact observed budgets and the no-headroom test; older untouched hotspots retain staged ratchet work. |
| T2 | Architecture breadth ratchet | DONE | Generic callable/class ceilings, merge-base widening checks, and current architecture lint are green. |
| T3 | PR CI | DONE | CI defines ordinary 3.12/3.13 gates, Python 3.12 full suite, critical contracts, native gates, and protected release certification. |
| T4 | Formatting/static reconciliation | DONE | Full Ruff format/check, MyPy, `scripts/static-critical`, compileall, and diff check are green. |
| T5 | Native quality gates | DONE | Native format, Clippy `-D warnings`, locked offline build/check/test are green (71 tests). |
| T6 | Merge-gate evidence | DONE | Cache-contract `make check` passed; required host-unavailable scenarios now fail the scenario gate until rerun on a capable host. |
| T7 | Warning budget | DONE | Late worker/loop teardown behavior is covered by concurrency and adversarial tests without warning regressions. |
| T8 | Commit/PR checkpoint | BLOCKED_IN_SANDBOX | `.git` is read-only in this sandbox. On a writable host, commit by independent seams: P0 fixes; concurrency retention; service ports; reality extraction; each remaining decomposition; CI/lint tightening; native extraction. Preserve unrelated dirty work and confirm each seam's focused lane before the next. |

## Remaining Execution Order

1. Continue D1–D7 and D8–D12 only where a concrete ownership boundary can be extracted; any budget change requires explicit review rationale.
2. Continue narrowing the native window runtime after the `WindowSession` and `FrameRuntime` seams, then lower the native ratchet again.
3. Narrow remaining service/lifecycle ports and split the remaining DualPane, broker fallback, workflow reconciliation, and synthesis runtime boundaries.
4. Keep the two full-suite MCP failures as host qualification items until a
   socket-capable host with the locked MCP 2.x package is available.
5. Re-read the original audit before claiming the entire decomposition roadmap complete; T8 remains host-blocked because `.git` is read-only here.

## Environment-Deferred / Known Runtime Notes

- Research TCP, SSH bwrap socket, wheelhouse network, and some synthesis timing
  scenarios remain host-dependent. Required missing host capability blocks the
  release scenario gate until host-capable evidence passes; do not relabel
  product failures as environmental.
- `test_provider_disconnect_after_candidate_mutation` passes but leaves an orphan
  `athena-blocking` thread warning after loop closure; retain as a known runtime
  cleanup note while the product path remains green.
- Native cadence evidence exists in `docs/native-cadence-benchmark.md`; rerun only
  on physical-display host if product evidence challenges the recorded baseline.

## DOX / Owner Contract Reminders

- Root rule: no second task/session/routing/execution/event-history/projection
  authority.
- Architecture baselines are ratchets. Widen only with explicit review
  rationale (the four correctness modules changed in this pass were explicitly
  authorized); prefer lowering after extraction.
- Keep private cross-package imports forbidden; extract at real ownership
  boundaries rather than wrappers.
- Update nearest owning `AGENTS.md` and Child DOX Index after each meaningful
  extraction. Ensure new child entries are unique and noncontradictory.
- UV command contract: `UV_CACHE_DIR=/tmp/athena-uv-cache uv run --extra dev ...`.
- Native command contract: use `--locked --offline` for locked build/check/test.

## Remaining Work Snapshot

- Correctness verification: **14 items**, all DONE with current focused evidence;
  full-suite recertification remains outstanding.
- Decomposition: **12 items**, all reviewed; D1–D11 remain partial and D12 remains the staged hotspot backlog, with additional routing, readiness, fallback, reflection, workflow-reconciliation, dispatcher-retry, lifecycle-port, startup-port, inference-retry, workflow-bootstrap, synthesis-admission, synthesis descriptor/result/support contracts, reality-route, reality-route-state, branch-registry, provider-stream, route-metadata, workflow-continuation, workflow-status, transaction-recovery, execution-routing, execution-session-lifecycle, workflow-step-preparation, workflow-run-query, task-intake-port, provider-construction-port, MCP-runtime-port, watcher-resource-port, provider-recovery-port, dual-pane terminal/animation lifecycle, SSH capability projection, framebuffer Buddy-placement/world rendering, pack-api, reflection-host-inventory, reflection-passport, reasoning-loop, projection execution-stream, reflection-permission, SSH transport, container transport, environment-probe, memory-embedding-index, memory-candidate-lifecycle, memory-retrieval-SQL, self-host operation-port, self-host completion-proof, TaskAPI operation-port, candidate operation-port, operator-query operation-port, interaction operation-port, dual-pane canonical-event projection, dual-pane stateless frame composition, retained Glass pixel presentation, durable startup-state acquisition, execution-startup graph acquisition, native X11 window/GLX setup, native X11 input/pointer/resize event ownership, native bounded text-width and Xft color-cache ownership, synthesis target revalidation, synthesis candidate construction, synthesis validation/admission, validated generated-runtime executor, authority-composition, direct-execution, and Hermes-runtime seams landed in this pass.
- Newly landed in this pass: bounded self-host planner invocation and plan-item preparation; stateless dual-pane chassis/control-rail composition; explicit TaskIntake facade use without partial-host lazy construction; RealityGate public route-state projections and canonical risk predicates; ExecutionManager direct routing/cancellation ownership plus execution-stream coordination; CapabilityFabric read-only search/ranking projection plus generated overlay lifecycle ownership; named candidate/interaction/operator-query mechanism operations; explicit direct-execution dispatcher/approval/transcript ownership; explicit authority composition; explicit Hermes runtime lifecycle; static synthesis descriptor plus neutral result/fixture/dependency support; bounded animated Buddy-world rendering; inference-prefix cache observation; conflict-aware memory write coordination; read-only memory-record queries, protocol-stable environment-record composition, and container session lifecycle/reattachment mechanics.
- Tooling/CI/hygiene: **8 items**, T1–T7 DONE and T8 host-blocked.
- The four correctness gaps above are fixed in the current working tree with
  focused evidence. Full Python completion remains unrecertified and host-
  limited by the two recorded MCP/loopback failures.
