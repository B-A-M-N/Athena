# Service Support Modules

## Purpose

Modules in this directory provide narrowly scoped runtime support owned by the
service composition lifecycle.

## Local Contracts

- `observations.py` translates terminal/debugger events and owns observation
  task-set mechanics only. It must not reason about or schedule autonomous work.
- `work_classification.py` owns deterministic admission classification
  (`WorkClass`, `SpeculationDepth`); it does not select models or plan work.
- `task_intake.py` assembles the canonical `AgentRequest` to `TaskSpec` record.
  AthenaService owns admission invariants; TaskIntake is a mechanism.
- `TaskIntakePorts` exposes only the service configuration/workspace resources
  and admission operations needed by `task_intake.py`.
- `direct_execution.py` owns the user-directed `!`/`!!` dispatcher call,
  approval replay, and session transcript persistence through
  `DirectExecutionPorts`; it does not execute outside the canonical
  dispatcher or become a second execution authority.
- `authority_composition.py` owns startup construction of memory, policy,
  dispatch, budget, task, and cancellation components through
  `AuthorityCompositionPorts`; lifecycle remains the startup transaction and
  state owner.
- `hermes_runtime.py` owns optional Hermes referee configuration, safety
  preflight, status projection, required-mode admission, and transport
  shutdown through `HermesRuntimePorts`; the facade's underscored Hermes
  properties and lifecycle methods are compatibility projections only.
- `self_host_continuation.py` owns persisted self-host mission continuation
  state transitions through `SelfHostPorts`; `SelfHostService` retains task
  admission, planning, and completion operation binding but not a second
  mission authority.
- Startup ordering, unwind, and composition authority remain in
  `lifecycle.py`; support modules receive explicit dependencies.
- Support facades are constructed explicitly in `AthenaService.__init__`.
  Do not reintroduce lazy `__getattr__` construction for partial test doubles;
  tests must construct only the explicit support objects they exercise.
- `interaction.py`, `candidates.py`, `operator_query.py`, `task_api.py`,
  `self_host.py`, and `recovery.py` access only the named resources exposed by
  their explicit ports (`InteractionPorts`, `CandidatePorts`,
  `OperatorQueryPorts`, `TaskAPIPorts`, `SelfHostPorts`, and
  `RecoveryPorts`). Private facade reach-throughs and unrelated attribute
  browsing are forbidden; owner seams are limited to application
  admission/submission, shadow/fusion factories, and background tracking.
- `lifecycle.py` accesses only the public names exposed by `LifecyclePorts`;
  `startup.py` uses the narrower `StartupPorts` acquisition/binding boundary
  through `ServiceLifecycle.ports`.
- `lifecycle_ports.py` is the explicit public-name read/write allowlist and
  owner-forwarding seam for lifecycle startup and teardown resources; its
  private owner names are implementation details and it must not become a
  general-purpose façade proxy.
- `StartupPorts` in `lifecycle_ports.py` owns the narrower startup acquisition
  and binding resource set; it must not absorb stop-only resources.
- `readiness.py` accesses only the named resources exposed by `ReadinessPorts`.
- `startup.py` coordinates ordered acquisition, binding, recovery, and worker startup; it does not own durable or execution state.
  The allowlist covers startup ordering resources and health state; browsing
  unrelated facade attributes is forbidden.
- `reasoning_components.py` composes model/context/verification/kernel
  resources from the explicit lifecycle port; it does not reorder startup or
  own service admission. The finalizer's documented owner handoff is the only
  application-owner boundary retained for checkpoint compatibility.
- `core_capabilities.py` owns the canonical core-capability wiring list and
  optional-capability health evidence; it never creates a second registry or
  authorizes execution.
- `interaction_capabilities.py` owns independent computer/browser optional
  registration and health boundaries; it uses the canonical registry and does
  not authorize interaction effects.
- `core_capability_ports.py` exposes the explicit resources and owner handoff
  required by core-capability wiring; registration must not browse the service
  façade's private fields directly.
- `stop_sequence.py` owns process/transport/store teardown mechanics under the
  lifecycle's stop ordering; it never restarts work or alters admission.
  It consumes the public `LifecyclePorts` names and preserves the façade as
  the sole state owner.
- `blocking_shutdown.py` drains the shared short/long blocking worker pools
  after capability teardown; it owns no task or execution state.
- `resource_finalizer_ports.py` exposes the task-owned resource close facts
  used by `resource_finalizer.py`; finalization remains the sole cleanup owner.
- `pack_api.py` owns operator-facing pack listing and lifecycle calls through
  explicit manager/configuration ports; it does not authorize contributions.
- `self_host_completion.py` owns executable performance proof, completion
  verification, and optional Hermes mission review through `SelfHostPorts`; it
  never changes mission status or grants promotion authority.
- `self_host_ports.py` owns the explicit self-host resource/application
  allowlist; it is not a mission or promotion authority.
- `self_host_verification.py` owns self-host checkout/toolchain validation,
  preflight records, and trusted verification-environment construction; it does
  not own missions.
- `self_host_planning.py` owns one bounded planner invocation and plan-item
  preparation through `SelfHostPorts`; it does not persist missions, grant
  completion, or promote candidates.
- `SelfHostService` exposes its planner, verification, completion, and Hermes
  mission-review mechanisms through named operations; callers must not use
  private helper reach-throughs.
- `fusion_composition.py` owns only explicit typed Fusion port construction; it
  does not own Fusion state, candidate selection, or execution authority.
- `startup_recovery.py` owns startup reconciliation of durable resources and
  execution state through explicit ports; lifecycle remains the health/status
  projection owner.
- `startup_state.py` owns database readiness and durable store-graph
  acquisition; startup ordering and health projection remain in the lifecycle
  runner.
- `startup_execution.py` owns execution backend/runtime registration and
  capability-health rehydration from explicit database/configuration inputs.
- `provider_runtime.py` constructs providers through `ProviderRuntimePorts`,
  limited to configured provider data and credential resolution.
- `mcp_runtime.py` uses `MCPRuntimePorts` for MCP resources, connection state,
  secrets, and reconnect/status callbacks; those ports expose public names and
  it does not browse the service facade.
- `task_intake_ports.py`, `provider_runtime_ports.py`, and
  `mcp_runtime_ports.py` hold the explicit support-module port contracts; they
  do not own admission, provider, or transport state.
- `watch_ports.py` exposes the watcher dispatcher, event, world-state, index,
  and registry resources; watcher polling does not browse the service façade.
- `provider_recovery_ports.py` exposes only provider-outcome stores, budget,
  task, kernel, event, and background-task resources; recovery does not browse
  the service façade.
- `RecoveryCoordinator` exposes named continuation, input-recovery, and pack
  quarantine operations; the facade retains only compatibility-level public
  application wrappers.
- `CandidateService` exposes named candidate selection, reviewer, diff,
  approval-match, and review-event operations; the facade's underscored
  wrappers are compatibility entrypoints and must not reach through private
  mechanism helpers.
- `OperatorInteractionService` exposes named approval recovery, grant
  installation, grant rehydration, and scope-clamping operations; the facade
  retains underscored compatibility wrappers only.
- `OperatorQueryService.invoke_synthesis` is the named synthesis dispatch
  operation; the facade's underscored wrapper is compatibility-only.
- `health.py` owns only the read-only live health projection; it does not
  mutate subsystem state, authorize work, or become a second readiness owner.
- `health_ports.py` exposes only the read-only live facts and status operations
  consumed by `health.py`; it must not become a readiness or mutation owner.
- `operational_matrix_ports.py` exposes only execution readiness and canonical
  projection operations used by `operational_matrix.py`.
- `TaskAPI.enqueue_spec` is the named enqueue operation; the facade's legacy
  `_enqueue_spec` wrapper exists only for application/test compatibility.

## Verification

- `pytest tests/unit/service -q`
- `./scripts/architecture-lint --quiet`
