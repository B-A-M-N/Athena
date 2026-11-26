# Capabilities

## Purpose

Capabilities expose governed, typed effects through Athena's canonical
invocation path.

## Ownership

`CapabilityDispatcher` coordinates admission, policy/effect evaluation,
approval, invocation, recording, batching, caching, and failure reporting.

## Local Contracts

- There is one canonical capability invocation path.
- Exact schemas and effect envelopes are enforced before execution.
- Policy and approval cannot be bypassed by generated, remote, research, or pack contributions.
- Capability implementations do not become reasoning authorities.
- Runtime complex-operation escalation records that a task needs a sticky
  candidate; RealityGate alone creates/translates/retains candidate workspaces.
- `PreparedCapabilityCall` is carried explicitly through dispatch; there is no
  ambient by-call-id prepared snapshot that could be replayed after reuse.

## Work Guidance

Extract dispatcher mechanisms as collaborators, not alternate dispatchers.
Keep generic schema compilation in a neutral utility below capability,
affordance, and workflow packages.

## Verification

Run capability, policy, security, and dispatch-many tests.

## Child DOX Index

| Path | Contract |
|------|----------|
| `research_contract.py` | Model-facing research schema and effect envelope; keep independent from implementation code. |
| `research.py` | Thin adapter that sends model requests to `research.service`. |
| `synthesis_operations.py` | Owned inspect/deprecate lifecycle handlers subordinate to `SynthesisCapability`. |
| `synthesis_admission.py` | Synthesis operation admission/dispatch coordination subordinate to `SynthesisCapability` and the canonical dispatcher. |
| `synthesis_descriptor.py` | Static model-facing synthesis descriptor/schema; no engine, fabric, policy, or execution authority. |
| `synthesis_results.py` | Canonical result construction shared by synthesis operation handlers; no admission or execution authority. |
| `synthesis_support.py` | Deterministic fixture/dependency merging shared by synthesis candidate workflows; no engine or registry authority. |
| `synthesis_candidate_builder.py` | Repair and contract-migration candidate construction; it performs no validation, registration, or promotion. |
| `synthesis_revalidation.py` | Revalidation workflow for unchanged generated capabilities; it owns no engine, fabric, or policy authority. |
| `synthesis_validation_admission.py` | Engine validation and generated-record admission; it delegates authority to the existing engine and fabric. |
| `dispatch_retry.py` | Bounded read-only invocation retry and diagnostic emission; subordinate to `CapabilityDispatcher`. |
| `capsule_effects.py` | Admission-time effect analysis for validated portable capsule graphs; no execution or policy authority. |
| `capsule_codec.py` | Content-addressed capsule encoding and validation; no capability or execution authority. |
| `workflow_effects.py` | Admission-time effect analysis for owner-scoped workflow graphs; no execution or policy authority. |
| `workflow_workspace.py` | Bounded off-loop disposable-workspace staging and cancellation-safe cleanup for workflow trials/replays. |
| `workflow_graph.py` | Owner-scoped reachable workflow graph loading for workflow admission and execution. |
| `dispatch_result_cache.py` | Health, result-observation, failure-memory, and bounded result-cache mechanics subordinate to `CapabilityDispatcher`. |
| `reflection_permissions.py` | Read-only policy/approval projection subordinate to `CapabilityReflection`; it grants no authority. |
| `reflection_inventory.py` | Bounded host/repository inventory projection subordinate to `CapabilityReflection`; it grants no authority. |
| `reflection_host_inventory.py` | Host-inventory composition subordinate to `CapabilityReflection`; it grants no authority. |
| `reflection_passport.py` | Bounded environment-passport section builders subordinate to `CapabilityReflection`; they grant no authority. |
| `reflection_passport_assembly.py` | Bounded environment-passport composition subordinate to `CapabilityReflection`; it grants no authority. |
