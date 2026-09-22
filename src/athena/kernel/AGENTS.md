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
