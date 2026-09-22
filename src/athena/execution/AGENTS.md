# Execution

## Purpose

Execution owns process trees, runtimes, PTYs, cancellation, and backend
contracts for governed effects.

## Ownership

`ExecutionManager` is the execution authority. Backends implement transport or
runtime mechanics; they do not create alternate execution paths.

## Local Contracts

- Every effect remains subject to policy and task scope.
- Process lifecycle, cancellation, reattachment, and receipts are observable.
- Backend-specific code must not decide kernel strategy or silently retry globally.

## Work Guidance

Split transports, sessions, dependencies, and remote runtimes by lifecycle
without duplicating the backend authority.

## Verification

Run execution unit tests, security tests, and relevant crash tests.

## Child DOX Index

| Path | Contract |
|------|----------|
| `ssh_remote_runtime.py` | Authenticated remote supervisor/relay programs only; no backend or execution authority. |
| `ssh_transport.py` | Authenticated SSH command, credential, supervisor RPC, and remote cleanup mechanics; subordinate to `SSHBackend`. |
| `ssh_profile.py` | Operator-owned SSH profile validation and remote-root safety; no transport or execution authority. |
| `ssh_session.py` | Authenticated remote worker/session wrappers and key cleanup; subordinate to `SSHBackend`, with no session registry authority. |
| `ssh_lifecycle.py` | Authenticated SSH worker creation and restart-time supervisor identity proof; subordinate to `SSHBackend`, with no session registry or execution-routing authority. |
| `ssh_capabilities.py` | Static authenticated SSH backend capability contract; no transport, session, or execution authority. |
| `container_transport.py` | Docker availability, image identity, container command, inspection, and cleanup mechanics; subordinate to `ContainerBackend`. |
| `container_session.py` | Docker-backed persistent worker/session mechanics; subordinate to `ContainerBackend`, with no session registry authority. |
| `container_lifecycle.py` | Docker session creation and durable reattachment validation through explicit backend hooks; subordinate to `ContainerBackend`, with no session registry or execution authority. |
| `environment_probe.py` | Bounded workspace-file and toolchain probes used to build environment fingerprints; no execution authority. |
| `environment_record.py` | Secret-free, protocol-stable environment description composition from already-probed inputs; no probing or execution authority. |
| `verification_environment.py` | Host-selected candidate-verification mounts, toolchains, and record validation; no candidate or execution authority. |
| `receipts.py` | Runtime-session and execution receipt storage; subordinate to `ExecutionManager`. |
| `result_buffer.py` | Bounded streamed-output collection into execution results; subordinate to `ExecutionManager`, with no routing or persistence authority. |
| `backend_catalog.py` | Backend registration, health inventory, and capability reflection; subordinate to `ExecutionManager`. |
| `backend_readiness.py` | Read-only backend capability and effective readiness queries through manager-owned resolver ports. |
| `runtime_readiness.py` | Read-only runtime availability and activity projection; subordinate to `ExecutionManager`. |
| `routing.py` | Runtime/backend registry and deterministic selection mechanics; subordinate to `ExecutionManager`, never an execution authority. |
| `cancellation.py` | Execution-owned session indexes and retryable cancellation reconciliation; subordinate to `ExecutionManager`. |
| `session_lifecycle.py` | Runtime/backend session creation, identity reattachment, late adoption, and cleanup after durable-start failure; uses `CancellationRegistry` indexes and has no execution authority. |
| `streaming.py` | Backend/runtime event normalization, session adoption, execution receipts, and stream observability; subordinate to `ExecutionManager`, with no routing or execution authority. |
| `shutdown.py` | Bounded shutdown of task sessions, runtimes, and backends through the manager's canonical cancellation callback; no routing or cancellation-state authority. |
