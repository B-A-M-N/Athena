# Agent Kernel

## Purpose

The kernel is Athena's single durable reasoning authority.

## Ownership

`AgentKernel` decides what happens next. Inference, continuation, finalization,
termination, dispatch, and lifecycle helpers implement mechanisms selected by
the kernel.

## Local Contracts

- Helpers may calculate, persist, invoke, validate, or reconcile; only the kernel chooses the next autonomous action.
- The router is injected; the kernel does not construct a fallback router.
- Kernel code must not depend on CLI, API, or service interfaces.
- Provider-specific mechanics belong to model/provider modules or the inference broker, not the reasoning policy.
- Generated implementation/contract failures may arm one bounded kernel-owned source-repair turn, but only the canonical `synthesis.repair` capability performs repair; dependency, environment, policy, and authority failures must follow their existing recovery mechanisms.
- Pending generated-recovery target evidence and allowance are persisted in task metadata and restored before the next turn; a restart must not reset or re-authorize the correction.
- Generated repair retries persist an in-flight marker before dispatch and are not replayed after an uncertain restart; recovery is required instead of duplicating the originating operation.
- `AgentKernel.run_task()` serializes entry per task so a durable input/approval wakeup cannot race a relaunch against the parked run.
- Generated recovery records retain task ID, objective, failing input, code hash, and the canonical repair call identity for model correction and audit.
- `adaptive_gate.py` projects typed admissible recovery classes for the primary loop; it never executes or promotes an action.
- The adaptive projection is advisory: implementation failures may expose `synthesis.repair`; dependency/environment failures expose revalidation; authority failures expose authority requests.

## Work Guidance

Extract by mechanism while preserving one decision authority. Do not create a
second loop in a helper or move decision-making into a facade.

## Verification

Run kernel unit tests, inference/routing tests, and `./scripts/architecture-lint --quiet`.

## Child DOX Index

| Path | Contract |
|------|----------|
| `inference_broker.py` | Provider attempt, durable receipt, budget accounting, and fallback mechanics; subordinate to `AgentKernel`. |
| `inference_identity.py` | Versioned, process-independent request and capability fingerprint canonicalization; no routing or retry authority. |
| `inference_replay.py` | Cached provider-response reconciliation and usage projection; subordinate to `InferenceBroker`. |
| `inference_attempt.py` | One provider-attempt invocation, accounting, and failure reconciliation; subordinate to `InferenceBroker`. |
| `inference_accounting.py` | Successful provider-attempt usage and budget reconciliation; subordinate to `InferenceBroker`. |
| `inference_fallback.py` | Next-candidate selection and context-window recompilation mechanics; subordinate to `InferenceBroker`. |
| `inference_retry.py` | Bounded retry eligibility and attempt sequencing; subordinate to `InferenceBroker`. |
| `inference_stream.py` | Provider stream consumption, response normalization, and receipt commit; subordinate to `InferenceBroker`. |
| `reasoning_loop.py` | The single model/capability loop mechanism invoked by `AgentKernel`; no independent authority. |
| `route_metadata.py` | Provider-route metadata and replay-boundary projection; subordinate to `InferenceBroker`. |
| `inference_prefix.py` | Cache-prefix tracker restoration, envelope observation, and boundary projection; subordinate to `InferenceBroker`. |
| `utility_inference.py` | Best-effort auxiliary request, usage, and cleanup mechanics; subordinate to `InferenceBroker`. |

- `recovery_dispatch.py` owns bounded speculative/generated failure evidence and next-turn admissibility checks; the kernel remains the sole action and finalization authority.
- `dispatch_corrections.py` owns bounded tool-input correction accounting; the kernel retains dispatch, escalation, and finalization.
- `observation_support.py` owns pure bounded observation/budget predicates; `observation_dispatch.py` offers at most one observation per dispatch. Both remain subordinate to the kernel's action authority.
- `retry_recovery.py` owns the durable one-shot repaired-operation retry and uncertain-outcome boundary; the kernel supplies dispatch and remains the action/finalization authority.
- `steering_support.py` owns materialization of already-authorized pending steering into durable transcript messages; the kernel remains the reasoning-boundary authority.
- `context_support.py` owns transcript loading, recovery/adaptive context hints, context compilation, and context evidence emission; the kernel remains the reasoning authority.
- `pack_hook_support.py` owns pack-hook workflow invocation validation/delegation; the kernel remains finalization and reasoning authority.
