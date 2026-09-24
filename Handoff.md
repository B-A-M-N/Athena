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
- The locked-extra full suite was rerun on a socket-capable host after the latest
  latest kernel/service/Fusion/CLI/verifier/cleanup/task-observation/user-turn/context/steering/resource-cleanup/workspace-reader changes:
  **2304 passed, 14 skipped, 0 failed** on the capable-host locked-MCP run. The ordinary sandboxed run blocks two
  MCP transport tests at socket creation; those exact tests pass **2/2** with
  the required local capability.
- Current focused evidence: 84 relevant tests pass across model/kernel,
  lock-cancellation, scenario/release, and release-evidence lanes; targeted
  Ruff, MyPy, compileall, `git diff --check`, and architecture lint (0
  violations) pass. The socket-capable full scenario rerun is **102/102 passed,
  0 failed, 0 missing, 0 skipped, 0 environment-unavailable**. The ordinary
  sandboxed scenario invocation cannot certify the three host-dependent cases
  or MCP-001 because it denies required local capabilities; that is an
  environment qualification, not a product failure.
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
  uncommitted; unrelated work was preserved. The current architecture lint is
  intentionally advisory: it reports size-ratchet debt and broad-catch review
  items, but no functional blocker from those arbitrary line limits.

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
| D1 | `RealityGate` | DONE | 194 / 269 class/module lines | Route classification, routing, transaction state/operations, branch registry, durable state, and public projections are separated; reality focused/recovery/concurrency tests pass. No current architecture-lint violation remains for this object. |
| D2 | `ExecutionManager` cancellation reconciliation | DONE | 203 / 249 class/module lines | `CancellationRegistry` owns execution/session indexes, pending persistence, reconciliation, retry state, and release semantics; manager exposes the canonical coordination methods and focused cancellation/concurrency tests pass. |
| D3 | `ExecutionManager` module/class | DONE | 385 / 440 class/module lines | Backend catalog/readiness, routing, receipts, session lifecycle, streaming, result collection, cancellation, and shutdown are explicit subordinate owners; the manager remains the single execution authority. Backend parity and execution regression lanes pass. |
| D4 | `lifecycle.py` startup phases | DONE | 347 / 399 class/module lines | Startup phase/state/execution/recovery/authority composition and teardown are explicit ports and subordinate owners; lifecycle tests, port-denial tests, and service startup lanes pass. |
| D5 | `x11.rs` event/window session | DONE | `x11.rs` 484; runtime 488; input 215; pointer 244; resize 94; setup 196; frame 212 | Event-loop session state, frame cadence, layout diagnostics, window/GLX setup, input/XIM, pointer/WM, resize, hints, and teardown have explicit native owners. Format, Clippy, locked check, and 71 native tests pass. |
| D6 | `render/text.rs` | DONE | 169 / 231 lines; width cache 58; color cache 84 | `FontCatalog`, `TextWidthCache`, and `XftColorCache` own Xft faces, bounded width/color lifetime, scale reset, and teardown; `TextRenderer` owns drawing/clipping. Native formatting, Clippy, check, and tests pass. |
| D7 | `AthenaService` / lifecycle reach-through | DONE | 1,773 / 2,361 class/module lines | The composition root now delegates lifecycle, readiness, intake/API, self-host, recovery, interaction, candidates, operator query, packs, provider/MCP, watchers, verification, task observation, steering, cleanup, profile, and workspace-reader mechanics through explicit ports/owners. Compatibility methods remain thin facade entrypoints; no private service reach-through remains in support modules. Current service/kernel/integration/crash lanes and release gates pass. |
| D8 | `InferenceBroker` `_invoke` | DONE | function 16 / 16; class 391 / 391 | Request preparation, attempt/accounting, fallback, retry, stream consumption, replay, route metadata, and prefix observation are explicit subordinate mechanisms; the broker remains the single inference coordinator and focused inference lanes pass. |
| D9 | `WorkflowRunStore` | DONE | class 259 / 489 | Identity/bootstrap, preparation, receipts, step transactions, reconciliation, continuations, status, and queries are explicit subordinate owners; the store remains the single workflow transaction coordinator and workflow/recovery lanes pass. |
| D10 | `SynthesisCapability` / synthesis surfaces | DONE | class 28 / 28 | Descriptor/schema, result/support contracts, admission/operations, candidate/revalidation, validation phases, and validated runtime ownership are explicit subordinate owners; synthesis remains task-scoped and promotion-gated, with 85 focused tests passing. |
| D11 | CLI god objects / surfaces | DONE | `OIFrameBuffer` 682 / 682; `DualPaneSurface` 487 / 487 | Framebuffer cache, placement, world pixels, and Buddy overlay encoding are explicit; `DualPaneEventProjection`, frame/chassis composers, Glass presentation, and lifecycle are separate owners. CLI instrument/dual-pane/mascot lanes pass 81 tests. |
| D12 | Other >600-line ratchets | DONE | `SSHBackend` 263; `ContainerBackend` 313; `ProjectEnvironmentFingerprint` 157; `MemoryStore` 360; `SelfHostService` 268; `SkillLifecycle` 350 class lines | SSH, container, environment, memory, self-host, and skill lifecycle classes now have explicit subordinate mechanism owners and behavior-focused tests. Remaining module/class size findings are stale advisory ratchets, not unowned behavior or correctness gaps. |

## Tooling / CI / Hygiene

| ID | Item | Status | Remaining work |
|---|---|---|---:|
| T1 | Exact ratchet-headroom tests | DONE | Completed seams have exact observed budgets and the no-headroom test; older untouched hotspots retain staged ratchet work. |
| T2 | Architecture breadth ratchet | DONE | The authority/ownership review is complete. Remaining size-budget findings are explicitly treated as advisory under the operator directive not to follow architectural line counts exactly. |
| T3 | PR CI | DONE | CI defines ordinary 3.12/3.13 gates, Python 3.12 full suite, critical contracts, native gates, and protected release certification. |
| T4 | Formatting/static reconciliation | DONE | Full Ruff format/check, MyPy, `scripts/static-critical`, compileall, and diff check are green. |
| T5 | Native quality gates | DONE | Native format, Clippy `-D warnings`, locked offline build/check/test are green (71 tests). |
| T6 | Merge-gate evidence | DONE | Socket-capable scenario evidence is **102/102 passed**; the ordinary sandboxed `make check` is blocked only by unavailable local host capabilities, while the exact MCP tests pass with loopback enabled. |
| T7 | Warning budget | DONE | Late worker/loop teardown behavior is covered by concurrency and adversarial tests without warning regressions. |
| T8 | Commit/PR checkpoint | BLOCKED_IN_SANDBOX | `.git` is read-only in this sandbox. On a writable host, commit by independent seams: P0 fixes; concurrency retention; service ports; reality extraction; each remaining decomposition; CI/lint tightening; native extraction. Preserve unrelated dirty work and confirm each seam's focused lane before the next. |

## Remaining Execution Order

1. Continue D1–D7 and D8–D12 only where a concrete ownership boundary can be extracted; any budget change requires explicit review rationale.
2. Continue narrowing the native window runtime after the `WindowSession` and `FrameRuntime` seams, then lower the native ratchet again.
3. Narrow remaining service/lifecycle ports and split the remaining DualPane, broker fallback, workflow reconciliation, and synthesis runtime boundaries.
4. Preserve the environment qualification: the socket-capable locked-extra run
   is **2304 passed, 14 skipped, 0 failed**. The ordinary sandboxed run blocks
   two MCP transport tests at socket creation; the exact pair passes **2/2**
   with local loopback sockets, and the socket-capable scenario gate is
   **102/102 passed**.
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
  the locked-extra full suite is current, with two sandbox-blocked MCP transport
  tests qualified by an exact **2/2** pass with loopback sockets.
- Decomposition: **12 items**, all reviewed and **DONE**; explicit support mechanisms cover the former facade/hotspot responsibilities. The original decomposition roadmap is complete; only stale size-ratchet debt and the sandbox-blocked commit/PR checkpoint remain.
- Newly landed in this pass: bounded self-host planner invocation and plan-item preparation; stateless dual-pane chassis/control-rail composition; explicit TaskIntake facade use without partial-host lazy construction; RealityGate public route-state projections and canonical risk predicates; ExecutionManager direct routing/cancellation ownership plus execution-stream coordination; CapabilityFabric read-only search/ranking projection plus generated overlay lifecycle ownership; named candidate/interaction/operator-query mechanism operations; explicit direct-execution dispatcher/approval/transcript ownership; explicit authority composition; explicit Hermes runtime lifecycle; static synthesis descriptor plus neutral result/fixture/dependency support; bounded animated Buddy-world rendering; inference-prefix cache observation; conflict-aware memory write coordination; read-only memory-record queries, protocol-stable environment-record composition, and container session lifecycle/reattachment mechanics.
- Newly landed after that snapshot: explicit live file-backed skill refresh, including unchanged-version conflict reporting and new-version-only lifecycle reconciliation; focused skills refresh evidence is 8 passing tests.
- Newly landed after that snapshot: branch-bound `synthesize_from_branch()` identity/workspace/provenance seam, with an 8-test Fusion integration lane passing.
- Newly landed after that snapshot: kernel-owned bounded generated-failure recovery state, restricted to typed implementation/contract evidence and the canonical `synthesis.repair` path; dependency, environment, policy, and authority failures do not arm source repair. Focused kernel lane: 11 passing tests.
- Newly landed after that snapshot: generated recovery is persisted on the task and reconstructed on resume, so the bounded allowance and target evidence survive a restart. Focused kernel lane remains 11 passing tests.
- Newly landed after that snapshot: durable workflow observation evidence proving first/second task inputs generalize to a typed parameter and the promoted procedure executes a third input after restart; focused workflow-store lane: 2 passing tests.
- Newly landed after that snapshot: skill promotion now resolves cited source-task and event evidence durably and fails closed on invented/missing references; focused skills/knowledge lane: 47 passing tests.
- Newly landed after that snapshot: successful canonical generated repair retries the original generated call once against the repaired capability and persists the retry boundary; focused kernel lane is 12 passing tests.
- Newly landed after that snapshot: explicit `SkillApplied` evidence is emitted on trigger, and learning attributes outcomes only to skills with matching ID/version application evidence; focused knowledge/skills lane: 13 passing tests.
- Newly landed after that snapshot: the focused correctness lane covering fusion, kernel recovery, skills, knowledge, workflows, dispatcher/conformance, and readiness/self-host contracts passes 176 tests. Full-suite and scenario recertification remain outstanding.
- Newly landed after that snapshot: ordinary context compilation now discovers relevant promoted workflows (including the persisted `PROMOTED` lifecycle), renders bounded ID/version/scope/input-schema suggestions, and keeps execution behind the workflow capability; focused context lane: 29 passing tests.
- Newly landed after that snapshot: bounded adaptive recovery projection distinguishes source repair, Fusion strategy change, and dependency/authority recovery; it is advisory metadata consumed by the existing kernel loop and never an execution authority. Focused adaptive-gate lane: 2 passing tests.
- Newly landed after that snapshot: focused context, adaptive-kernel, generated-recovery, knowledge, skills, and workflow regression lane passes 60 tests after the M-05/M-07 changes.
- Newly landed after that snapshot: composed C-03 acceptance now runs the real service-owned Fusion, Shadow, independent verification, RealityCoordinator, and commit seam; the full Fusion integration file passes 9 tests.
- Newly landed after that snapshot: the typed adaptive decision is rendered as bounded advisory task context on resumed turns; focused context lane is now 28 passing tests.
- Newly landed after that snapshot: skill refresh emits a durable `SkillFilesRefreshed` event with installed/conflict counts for operator auditability; skills refresh and lifecycle tests remain green.
- Newly landed after that snapshot: the durable refresh-event path has direct regression coverage; latest changed-area lane is 61 passing tests.
- Newly landed after that snapshot: C-13 bounded parallel comparison is opt-in through `parallel`/`max_parallel` (capped at four), preserves isolated branches, serialized selection, and `reality_mutated=false`; capability/Fusion lane: 14 passing tests.
- Newly landed after that snapshot: M-06 scope-widening promotion now requires multiple resolved observations and at least two observed environments; focused skill lifecycle lane: 6 passing tests.
- Newly landed after that snapshot: C-13 parallel comparison and M-06 widening-evidence changes are included in the latest combined focused regression lane: 81 passing tests.
- Newly landed after that snapshot: generated-repair retry is durably marked `dispatching` before dispatch, becomes `consumed` only after canonical dispatch returns, and uncertain restart state becomes `RECOVERY_REQUIRED`; kernel lane remains green.
- Newly landed after that snapshot: uncertain generated-retry restart behavior has direct regression coverage; combined kernel lane is 12 passing tests.
- Newly landed after that snapshot: per-task kernel run serialization closes the live input-wakeup/relaunch race; the input-request integration file now passes 2/2 and the kernel regression lane passes 26 tests.
- Newly landed after that snapshot: M-03 composed branch synthesis is exposed through the governed Fusion capability and admits a validated tool into the task-local Fabric; focused Fusion acceptance lane: 2 passing tests.
- Newly landed after that snapshot: composed generated-repair acceptance now uses the real SynthesisCapability/Fabric path, preserves the failing input, revalidates the repair, and retries the original operation once; focused kernel acceptance: 1 passing test.
- Newly landed after that snapshot: Fusion's production composition consumes explicit task/event/runtime/context/fabric/workflow/synthesis/dispatcher/verification/reality/budget ports, removes whole-service reach-through from the typed path, and delegates bounded semantic checkpoint evidence to `semantic_snapshot.py`; focused Fusion lane: 19 passing tests.
- Newly landed after that snapshot: generated validation now coordinates explicit static, dependency, sandbox, and evidence phases through `validation_phases.py`; the focused synthesis and synthesis-capability lane passes 85 tests, with the 233-line `Validator.validate()` method reduced to a phase coordinator.
- Newly landed after that snapshot: branch-bound generated admission now lives in `branch_synthesis.py`, including exact branch/workspace/fingerprint/environment checks and task-overlay admission; Fusion remains the sole experiment/reality coordinator. Focused Fusion lane: 13 passing tests.
- Newly landed after that snapshot: declarative invariant probe construction and canonical shadow dispatch now live in `invariants.py`; the orchestrator only decides when the resulting invariant set is checked. Focused Fusion/capability lane: 25 passing tests.
- Newly landed after that snapshot: transparent Buddy overlay PNG encoding now lives in `framebuffer_overlay.py`; `framebuffer.py` retains static scene, motion-layer, cache, and placement ownership. CLI instrument/dual-pane/mascot lane: 81 passing, 10 skipped.
- Newly landed after that snapshot: `ReasoningSupport` now owns typed model-role policy normalization and late-bound summarizer/interpreter callbacks; `AthenaService` retains the single router construction site and compatibility entrypoints. Focused service/router/interpreter lane: 35 passing.
- Newly landed after that snapshot: configured capability-profile validation now lives in `capability_profile_support.py`; startup remains the only caller and missing/unhealthy requirements stay blocking. Focused profile/lifecycle lane: 14 passing.
- Kernel decomposition: recovery evidence/admissibility, tool-input correction accounting, and one-observation interpreter admission are separate mechanisms; the kernel is now 2046 module / 1321 class lines and `_dispatch()` is below the callable ratchet.
- Newly landed after that snapshot: bounded tool-input correction accounting now lives in `dispatch_corrections.py`; the kernel retains dispatch, escalation, and finalization. Focused kernel/fusion/adaptive lane: 30 passing.
- Newly landed after that snapshot: bounded observation admission and interpreter offer selection now live in `observation_dispatch.py` and pure predicates in `observation_support.py`; the kernel retains dispatch, proposal execution, and finalization. Focused kernel/fusion/adaptive lane: 30 passing.
- Newly landed after that snapshot: durable one-shot repaired-operation retry and uncertain-outcome accounting now live in `retry_recovery.py`; the kernel supplies dispatch and retains finalization. Focused kernel/fusion/adaptive lane: 30 passing.
- Newly landed after that snapshot: task forensic event taxonomy projection now lives in `task_inspection.py`; `AthenaService.inspect()` remains a compatibility entrypoint over canonical events. Focused service lane: 26 passing.
- Newly landed after that snapshot: bounded verifier evidence projection now lives in `verification_support.py`; the service compatibility method and canonical verifier authority remain intact. Focused service/verifier lane: 173 passing.
- Follow-up: `verification_support.py` now also owns composite-verifier construction and persisted self-host verification-environment revalidation; focused service/provider/self-host/capability lane: 27 passing.
- Newly landed after that snapshot: bounded speculative/generated recovery evidence and next-turn admissibility checks now live in `recovery_dispatch.py`; the kernel retains compatibility wrappers, action selection, dispatch, and finalization. Focused kernel/fusion/adaptive lane: 29 passing tests.
- Correctness follow-up: recovery-repeat detection now compares the same canonical SHA-256 fingerprint used by the failure ledger; direct regression test passes.
- Current verification: Ruff format/check, full MyPy, compileall, static-critical,
  and diff check pass; native format, Clippy, locked offline check, and 71 native
  tests pass. The socket-capable locked-extra full suite records **2304 passed,
  14 skipped, 0 failed**. The ordinary sandboxed run also blocks the two MCP
  transport tests at socket creation; the exact pair passes **2/2** with loopback
  sockets enabled. The input-resume race is fixed with a 30-service stress
  reproduction and 2/2 integration tests passing. The current scenario gate is
  **102/102 passed, 0 failed, 0 missing, 0 skipped, 0 environment-unavailable**.
  Final changed-area lane: **99 passed**; post-M-03/M-01 verification lane:
  **78 passed**.
- Latest locked-MCP full suite: **2304 passed, 14 skipped, 0 failed** after the
  latest kernel/service/Fusion/CLI changes. The exact MCP pair passes **2/2** with
  loopback sockets enabled.
- Final changed-area regression lane: **97 passed**.
- Tooling/CI/hygiene: **8 items**, T1–T7 DONE and T8 host-blocked; no code or release evidence remains outstanding.
- The four correctness gaps above are fixed in the current working tree with
  focused evidence. The full Python suite and socket-capable scenario gate are
  green; the only remaining qualification is the two MCP transport tests when
  run without required local loopback capability. The latest full-suite count
  is **2304 passed, 14 skipped, 0 failed**.
- Newly landed after that snapshot: post-mutation project-index and task-world-state invalidation now lives in `mutation_support.py`; durable claim state remains the world-state authority. Focused service/crash lane: 18 passing.
- Newly landed after that snapshot: task-scoped affordance cleanup now lives in `affordance_support.py`; finalization remains the task manager authority. Focused service/crash lane: 149 passing.
- Current final evidence after task-observation/affordance/user-turn/context/pack-hook/steering/resource-cleanup/workspace-reader/profile/skill-candidate extraction: capable-host locked-MCP **2304 passed, 14 skipped, 0 failed**; installed-artifact acceptance passes after the canonical `SessionRepository.list_all()` correction. The combined service/kernel/integration/crash lane passes **153 tests**; the pack-hook focused lane passes **89 tests**; workspace-reader lane passes **30 tests**; profile lane passes **17 tests**; skills lane passes **19 tests**; capable-host scenario gate **102/102 passed**. A subsequent ordinary sandboxed run reports **2294 passed, 22 skipped, 2 socket-blocked MCP tests**; the exact MCP pair passes **2/2** with sockets enabled.
- Newly landed after that snapshot: task/session/result/event observation now lives in `task_observation.py`; `TaskAPI` remains intake/admission and durable stores remain authoritative. Focused service/integration/crash lane: 155 passing.
- Newly landed after that snapshot: canonical durable user-turn persistence now lives in `user_turn_support.py`; late-bound message-store wiring is preserved, and intake/recovery remain authoritative. Focused intake/knowledge/resume lane: 13 passing.
- Newly landed after that snapshot: context compilation/transcript loading/recovery hints/evidence emission now live in `context_support.py`; the kernel retains the reasoning loop. Focused kernel/integration lane: 90 passing.
- Newly landed after that snapshot: pack-hook workflow invocation/delegation now lives in `pack_hook_support.py`; the kernel retains finalization and reasoning authority. Focused kernel/pack-hook lane: 89 passing.
- Newly landed after that snapshot: task steering ancestor authorization/status gating/durable enqueue now lives in `steering.py`; the service remains the compatibility facade. Focused service/integration lane: 149 passing.
- Newly landed after that snapshot: durable resource-cleanup retry and pending-finalization commit evidence now live in `resource_cleanup_support.py`; finalization remains the task authority. Focused service/crash lane: 12 passing.
- Newly landed after that snapshot: hierarchical AGENTS.md reading/snapshot projection now lives in `workspace_reader.py`; the service retains the compatibility callback. Focused context/resume lane: 30 passing.
- Newly landed after that snapshot: synchronous live capability-profile projection now lives in `capability_profile_support.py` alongside async validation; the service retains the compatibility callback. Focused profile/lifecycle/knowledge lane: 17 passing.
- Newly landed after that snapshot: live capability-profile status projection now shares `CapabilityProfileSupport` with async validation; the service retains the compatibility callback. Focused profile/lane: 17 passing.
- Newly landed after that snapshot: skill candidate queue/evidence/promotion mechanics now live in `candidate_lifecycle.py`; `SkillLifecycle` remains the active-skill authority and scope/evidence/revision gates are preserved. Full skills unit lane: 19 passing.
- Newly landed after that snapshot: skill candidate persistence/evidence/promotion now lives in `candidate_lifecycle.py`; `SkillLifecycle` remains the active-skill authority and all scope/evidence/revision gates are preserved. Full skills unit lane: 19 passing.
