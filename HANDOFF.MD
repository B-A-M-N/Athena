# Athena — Capability Integration and Refinement: Handoff

## Handoff status

This handoff records the capability-integration remediation currently in the
working tree and the work that remains before the adaptive capability path can
be called release-ready.

The implementation preserves the existing authority model:

- `AgentKernel` remains the only reasoning authority.
- Fusion supplies isolated execution and evidence; it does not promote or
  commit candidates.
- Synthesis validates and repairs generated tools when explicitly selected;
  it does not run a second autonomous repair loop.
- Skill lifecycle owns persistence, version checks, validation and explicit
  promotion.
- Reality and policy boundaries remain authoritative for external mutation.

Architecture size ratchets were not widened. Existing size-budget findings
from `architecture-lint` remain release hygiene work, not a reason to change
the budgets.

## Completed in the current remediation

### Routing and lifecycle identity

- Strategy selection now classifies task work separately from visible
  affordances. A simple direct edit remains direct even when Fusion and
  synthesis are registered.
- Advanced routes require task suitability before their relative utility is
  considered; unrelated inventory entries no longer increase their score.
- Skill refinement candidates persist `target_skill` and
  `target_skill_version` through migration, restart and reconstruction.
- Promotion verifies that the target still exists at the expected version and
  reports a revision conflict instead of overwriting a concurrent edit.

### Evidence and generated-tool integrity

- Positive sandbox validation requires a valid result envelope, including an
  explicit JSON `null` for an intentional `None` result.
- Missing, malformed or incompatible result framing fails closed.
- Derived proof cases are executed where supported and remain explicitly
  unverified where no valid oracle can be inferred.
- Authored fixtures, derived cases, historical regressions and independent
  behavioral evidence remain distinguishable.
- Regression records can carry behavioral oracles and original-failure
  reproduction state; execution recovery without a trustworthy oracle is not
  reported as semantic resolution.
- Failure classification now carries structured recovery guidance for
  implementation, contract, dependency, stale-evidence, policy and
  infrastructure failures.
- Proof records distinguish execution success, contract success, semantic or
  downstream verification, reuse and observed task benefit.

### Bounded speculative recovery

- Fusion failure results carry the failed operation, verification results,
  invariant violations, workspace changes, branch identity and remaining
  budget in a structured failure record.
- The kernel records that evidence and may request one bounded continuation
  attempt. A continuation must identify a materially different approach with
  `changes_from_previous` and a different proposal fingerprint.
- Unchanged proposals, exhausted budgets and repeated non-progress terminate
  the recovery path. Fusion does not invent the correction.
- A deterministic kernel test exercises the failure → changed proposal →
  normal candidate path, and the held-out benchmark covers enabled versus
  disabled bounded speculation.

### Skill evidence and feedback

- Skill candidates now contain operational procedure information: conditions,
  prerequisites, ordered calls, parameter relationships, observations,
  verification, recovery and stopping guidance.
- Task-local operations are not silently embedded in portable skills.
- Skill selection incorporates applicability, scope, prerequisites, observed
  reuse and known failures after the lexical first pass.
- Context records the exact skill ID/version selected for a task.
- Post-task outcomes are attributed to that exact version. Failed reuse can
  create a targeted, unpromoted refinement candidate instead of an unrelated
  new skill.
- Heuristic candidate confidence is treated as proposal evidence, not as a
  calibrated reliability claim.

### Execution boundary work retained in this commit

The working tree also contains the preceding execution-isolation and resource
enforcement corrections across backend, container, process-tree, runtime and
SSH paths, plus their conformance/readiness tests. Those changes remain
release prerequisites and need the full post-change gate below; passing the
focused Python suite is not by itself a certification of those boundaries.

## Remaining work before release

The following items are intentionally not represented as complete merely
because their plumbing exists.

1. **C-03 composed Fusion acceptance.** The kernel recovery seam is covered
   with a controlled dispatcher. Add a composed-service test using the real
   `FusionOrchestrator`, `ShadowEngine`, independent verifier and existing
   RealityCoordinator boundary to prove that the corrected candidate is
   isolated, verified and reaches the normal selection/commit seam.

2. **C-04 generated-tool repair loop.** Structured generated-tool diagnostics
   exist, but the kernel does not yet perform a complete bounded repair,
   revalidation and originating-task continuation for a previously unseen
   valid input. Add that kernel-owned flow. Route dependency and policy
   failures away from source repair.

3. **C-06 Fusion-to-synthesis branch binding.**
   `synthesize_from_branch()` still needs an explicit branch/workspace
   parameter, validation through that exact workspace, and provenance binding
   to branch identity, workspace fingerprint and validation environment. The
   normal dispatcher should remain the route for task-local synthesis.

4. **C-10 live file-backed skill refresh.** The loader detects file revisions
   when loaded, but the running service still needs an inexpensive explicit
   refresh trigger and a version-conflict report for changed content with an
   unchanged `(name, version)` pair. Active task context must remain stable
   for the current turn.

5. **C-11 promotion evidence resolution.** Structural candidate validation is
   not yet a complete independent resolution of cited task and verification
   receipts. Add durable-reference checks and stronger evidence requirements
   when a candidate widens its applicability.

6. **C-12 complete cross-capability evidence coverage.** The principal
   speculative, generated-tool and skill handoffs now carry typed evidence,
   but audit all remaining seams for task/artifact identity, revision, scope
   and verification status. Events must remain evidence, never permission.

7. **C-13 controlled parallel comparison.** Fusion comparison is still
   sequential. If measured latency justifies it, add bounded parallel
   execution only for immutable-base, separate-workspace candidates with
   independent budgets. Selection, stale-base checks and promotion remain
   serialized.

8. **C-14 improvement flow certification.** Add composed tests proving that
   unavailable dependencies do not trigger recursive generation, while a
   genuine implementation failure receives at most the permitted bounded
   corrective workflow.

9. **Full acceptance matrix and live-model evidence.** The deterministic
   benchmark is not a substitute for a smaller live-model set on unfamiliar
   tasks. Record completion, independent verification, unnecessary capability
   creation, route choice, failed promotion, repeated regressions, latency,
   resource cost and recovery success.

10. **Repository release gates.** Re-run and resolve the applicable results of
    `make check`, native build/check/test, architecture lint, and the full
    Python suite after the final changes. The prior `make check` scenario
    runner reported 102/102 scenarios passed but the umbrella command did not
    finish cleanly in the available run. Do not report that gate as green until
    the command completes.

## Verification recorded for this handoff

The following checks passed after the latest edits:

- `uv run --extra dev ruff check src tests`
- `uv run --extra dev mypy src/athena` — 562 source files
- Focused Python suite including kernel, skills, context, knowledge, Fusion,
  synthesis capability and the new reliability benchmark: **262 passed**
- `PYTHONPATH=src pytest -q tests/e2e/test_capability_reliability.py` — **1
  passed**
- `git diff --check`

Previously recorded in this remediation, before the final handoff edits:

- Broader affected Python suite: **323 passed**
- Native offline build and tests: build passed; **8 library + 63 main tests**
  passed
- Scenario runner invoked by `make check`: **102 passed, 0 failed, 0
  missing, 0 skipped, 0 environment_unavailable**; the enclosing command was
  interrupted after the runner did not return promptly.

Known non-green status: architecture lint reports 70 violations, primarily
size-budget ratchet findings, plus the existing Fusion private-owner
reach-through family (including one migration-ratchet increase). The size
budgets were intentionally left unchanged per the repository contract and
user direction; the non-budget reach-through cleanup remains part of the
architecture follow-up and is not represented as fixed here.

## # Athena — Capability Integration and Refinement: Final Code Edit Report

The deeper inspection found that Athena is substantially integrated for
executing capabilities, while the feedback loops for selecting, combining and
improving capabilities were less complete. The correct remedy is refinement at
the existing boundaries, not another orchestrator or independent agent.

### C-01 — Task-based strategy selection — implemented

Files: `src/athena/strategy.py`, `src/athena/context/compiler.py`, related
strategy tests.

Work requirements are classified separately from capability availability.
Fusion and synthesis are admitted only when the task warrants them, and
unrelated registered capabilities no longer inflate route scores. The README
simple-edit regression is covered.

### C-02 — Preserve skill refinement identity — implemented

Files: `src/athena/skills/models.py`, `src/athena/skills/lifecycle.py`,
`src/athena/state/migrations/052_skill_candidate_targets.sql`, migration
digests and lifecycle tests.

Target skill ID and expected version survive persistence and restart. Promotion
checks the exact revision and exposes missing-target and revision-conflict
outcomes rather than silently installing a duplicate.

### C-03 — Bounded speculative correction — partially implemented

Files: `src/athena/fusion/orchestrator.py`, `src/athena/capabilities/fusion.py`,
`src/athena/kernel/kernel.py`, `src/athena/protocol/failure.py`, comparison
and kernel tests.

Fusion now returns typed failure evidence and the kernel owns a bounded,
materially-different continuation decision. The remaining gap is the composed
real-Fusion acceptance scenario described above.

### C-04 — Operational generated-tool repair — partially implemented

Files: `src/athena/synthesis/executor.py`, `src/athena/synthesis/engine.py`,
`src/athena/synthesis/runtime.py`, synthesis admission and kernel evidence
paths.

Failure categories, recovery actions, regression inheritance and revalidation
evidence are present. The missing release behavior is one kernel-owned,
bounded repair continuation for an unseen valid input, with dependency and
policy failures routed to their proper boundaries.

### C-05 — Behavioral generated-tool verification — implemented at the
current evidence boundary

Files: `src/athena/synthesis/validation.py`, `proof_corpus.py`,
`verifier.py`, `proof_ledger.py`, sandbox and synthesis tests.

Valid result framing, semantic verification categories, derived-case
execution, historical regressions and independent evidence are distinct. The
remaining release requirement is broader project-input coverage in composed
acceptance tests, not another boolean validation flag.

### C-06 — Fusion-to-synthesis shadow provenance — outstanding

File: `src/athena/fusion/orchestrator.py`.

The existing helper still needs exact branch/workspace inputs and validation
provenance. A task-local generated tool must not escape its candidate scope or
be treated as globally verified merely because the helper was called.

### C-07 — Reusable procedure extraction — implemented with evidence limits

Files: `src/athena/skills/candidates.py`, `src/athena/knowledge/pipeline.py`.

Candidates now describe prerequisites, parameterized operations, observations,
verification, failure responses and stopping conditions. Repository-specific
paths are not intended to become portable literals. Candidate confidence is
heuristic proposal confidence; it is not measured reliability.

### C-08 — Evidence-driven skill refinement — implemented for selected reuse

Files: `src/athena/knowledge/pipeline.py`, `src/athena/skills/candidates.py`,
`src/athena/skills/lifecycle.py`, knowledge and lifecycle tests.

Exact selected skill versions are attributed to outcomes. Failed reuse can
produce a targeted unpromoted successor, while successful reuse adds evidence
without rewriting the active skill. Cross-environment accumulation and
conflicting observations still need the live acceptance matrix.

### C-09 — Context-sensitive skill selection — implemented at the bounded
selector layer

Files: `src/athena/skills/selector.py`, `src/athena/context/retrieval.py`,
`src/athena/context/compiler.py`.

Lexical discovery is followed by scope, applicability, environment,
prerequisite, reuse and failure checks. False-positive, competing-skill and
unavailable-prerequisite tests are included in the focused suite. Dynamic
mid-service file refresh remains C-10.

### C-10 — File-backed skill refresh — outstanding

Files: `src/athena/service/authority_composition.py`,
`src/athena/service/service.py`, `src/athena/skills/loader.py` and lifecycle.

Startup synchronization and loader revision detection exist. A running
service still needs an explicit low-cost synchronization path, stable current
turn context and an explicit same-version content conflict.

### C-11 — Skill-promotion evidence — partially implemented

File: `src/athena/skills/validator.py` and lifecycle.

Structural hygiene and candidate confidence are checked, but cited durable
task and verification records are not yet independently resolved for every
promotion scope. Invented evidence IDs must remain unproven rather than being
reported as behavioral verification.

### C-12 — Typed evidence handoffs — substantially implemented

Files: kernel, affordance/context paths, Fusion ports, synthesis proof ledger
and knowledge pipeline.

Task, artifact, branch/revision, scope and verification metadata now travel
through the main diagnostic and skill-outcome paths. Complete the seam audit
before release and keep evidence consumers independently authoritative.

### C-13 — Bounded independent parallelism — outstanding

Files: `src/athena/fusion/comparison.py`, task budgets and reality gate.

Comparison remains sequential by design. Parallelism should be added only if
measured latency warrants it, with isolated workspaces, immutable bases,
explicit budgets and serialized selection/promotion.

### C-14 — Non-recursive improvement flow — partially implemented

Files: strategy, kernel reasoning/continuation and interpreter triggering.

The bounded speculative recovery state prevents open-ended retries and the
diagnostic categories identify recovery classes. Complete dependency-versus-
repair composed tests before treating the flow as certified.

## Intended task flow

1. The kernel understands the objective and inspects available capabilities.
2. It chooses the smallest suitable existing mechanism.
3. If a real capability gap is proven, it creates and validates a task-local
   tool through the governed dispatcher.
4. It executes directly or in an isolated candidate according to risk.
5. Independent verification and bounded correction produce evidence.
6. RealityCoordinator remains the only candidate-promotion/commit boundary.
7. Durable results, regression cases and procedural observations become future
   evidence.
8. Reusable tools and skills are proposed, validated and explicitly promoted.

No newly generated capability should automatically trigger another generation,
speculative fork or promotion process.

## Required end-to-end matrix

| Scenario | Required result |
|---|---|
| Simple edit with advanced capabilities registered | Direct execution; no unnecessary Fusion or synthesis |
| Missing utility during a larger refactor | Task-scoped tool is generated, validated and kept isolated |
| Tool passes examples but fails in real use | Failure retained; one bounded repair; independent verification rerun |
| Candidate fails a project test | Specific evidence returned; failed branch never committed |
| Two tasks use one procedure | Evidence accumulates into one appropriate candidate |
| Existing skill fails in a new environment | Failure retained; versioned refinement proposed; prior version preserved |
| Capability becomes unavailable mid-task | Readiness refresh and admissible alternative without losing task state |
| Two competing candidates | Separate certificates; explicit verified selection; stale selection rejected |
| Restart before promotion | Exact target, version, evidence and approval restored without duplicate effect |
| Repeated nonproductive improvement | Stop within budget and report the unresolved problem |

Run the matrix against the composed service and actual dispatcher. Deterministic
fixtures establish routing and state transitions; a smaller live-model set is
needed to measure behavior on unfamiliar tasks. Completion and independent
verification must be reported separately from capability usage frequency.

## Release boundary

The recommended order is:

1. Keep C-01 and C-02 as correctness prerequisites.
2. Finish C-03 through C-08 bounded recovery and behavioral evidence.
3. Finish C-09 through C-12 relevance, refresh and evidence durability.
4. Only then consider C-13 parallelism and certify C-14 flow control with the
   full end-to-end matrix.
5. Re-run all execution-isolation and repository release gates after the final
   implementation changes.

The practical target is a bounded system that recognizes an applicable
capability, uses it, determines whether the intended task actually succeeded,
and retains a verified correction when it did not. It is not unrestricted
self-improvement.

## Addendum — Remaining adaptive mechanics at commit `1a18021`

The missing mechanics are the more important part of the follow-up review.
At commit `1a18021`, Athena can create tools, run speculative candidates,
learn skills and store reusable workflows, but three essential adaptive loops
are still incomplete.

### The three implementation gaps that matter most

1. **Complete tool-repair execution.** A generated tool can report a
   structured failure and retain regression evidence. What is still missing
   is a fully integrated kernel-owned path that takes an unfamiliar failure,
   develops a corrected implementation, revalidates it and resumes the
   original task. The individual mechanisms exist; the complete adaptive
   journey remains unfinished.

2. **Genuine outcome-based learning.** Athena records outcomes for skills that
   were included in a task's context. That is not proof that the agent
   actually followed those skills or that they caused the outcome. Without a
   stronger attribution mechanism, the system can learn misleading lessons
   and refine the wrong skills.

3. **Durable adaptive recovery.** Fusion supports bounded correction of failed
   speculative candidates, but its recovery attempts and pending state are
   held in the kernel's in-memory `RunState`. The reconstruction path does not
   yet restore that adaptive state after a process restart. Durable tasks
   therefore do not yet have fully durable improvement cycles.

These are missing behavioral mechanisms, not requests for more abstractions.
The first lets Athena improve something during a task; the second lets it
learn whether that improvement helped; the third lets it continue the same
improvement process safely after interruption.

## Newly confirmed missing workflow mechanic

### P0 — Learned workflows do not reliably generalize their inputs

Files: `src/athena/workflows/store.py`, `src/athena/workflows/mining.py`

Athena has a mechanism for turning repeated successful procedures into
parameterized workflows. If two tasks successfully read `one.py` and
`two.py`, it should replace the literal filename with `$input.path` and
generate an input schema.

The durable observation path currently constructs a candidate with both task
IDs already marked as observed, then calls `merge_observation()`. That
function immediately ignores a task ID already present in the candidate. The
second observation is skipped instead of generalizing the differing value.

Observed bad state:

```text
path: "one.py"
input_schema: {}
```

Required state:

```text
path: "$input.path"
input_schema: {"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}
```

Required edit: In `WorkflowStore.save_pending_observation()`, initialize the
candidate from the first observation only. Apply subsequent observations in
order so `merge_observation()` can register each new task ID and generalize
differing values. Alternatively, implement a pure generalization function
over the complete observation set, separate from the duplicate-observation
guard.

Add a database-backed regression test with two different input values, a
service restart, candidate promotion and execution with a third input.

The database-backed reproduction confirms that this occurs in the actual
learning path, not just in a helper: after two successful observations the
candidate is marked ready, but its input schema remains empty and its step
still contains `first.py`.

Workflow execution, input binding, promotion-time replay validation and
portable procedure capsules already exist. They do not need to be invented;
the missing work is connecting discovery, correct generalization and
subsequent reuse into a reliable learning cycle.

## Remaining implementation mechanics

### M-01 — Complete the generated-tool repair cycle

**Priority:** Essential

Files: `kernel/kernel.py`, `synthesis/executor.py`,
`capabilities/synthesis_candidate_builder.py`,
`capabilities/synthesis_revalidation.py`

When generated execution reports an implementation failure, the existing
kernel must retain the exact tool revision, failing input, dependency
fingerprint, error and original task objective. It should present that
evidence to the model for a proposed correction and submit the correction
through the existing `synthesis.repair` operation.

Revalidation must execute inherited regression cases, the newly observed
failure and independent acceptance checks. If successful, retry the suspended
original operation once against the corrected candidate. If unsuccessful,
request a materially different approach within budget or terminate with
unresolved evidence. Dependency errors, policy denials and stale environments
must follow their existing mechanisms rather than triggering source rewrites.

Completion criterion: a generated parser that works initially but fails on an
unfamiliar valid input is diagnosed, repaired, independently verified and
finishes the original task without manually scripted tool calls.

### M-02 — Make adaptive decisions survive a restart

**Priority:** Essential

Files: `kernel/kernel.py`, `kernel/continuations_coordinator.py`, existing
durable task and event stores.

Persist the adaptive transition before dispatching a corrective attempt.
Record failure identity, proposal fingerprint, remaining allowance and
pending action under the canonical task ID. On restart, reconstruct that state
from committed records and reconcile any already-dispatched operation before
allowing another attempt.

Use the same durable continuation mechanism for generated-tool repairs. A task
must not acquire inconsistent recovery budgets merely because it moves from
Fusion into synthesis.

Completion criterion: killing Athena between a failed candidate and its
correction, and again after a repair is dispatched but before its result is
consumed, neither duplicates an effect nor resets the corrective-attempt
allowance.

### M-03 — Integrate tool creation inside speculative work

**Priority:** Essential for combined operation

Files: `fusion/orchestrator.py`, `fusion/ports.py`, the existing synthesis
capability and canonical dispatcher.

Extend `synthesize_from_branch()` to accept branch identity, effective
workspace and validation context. Route generated-tool creation through
canonical dispatch, retain task-local scope and bind proof to the exact
speculative workspace fingerprint.

The kernel should be able to choose tool creation when a speculative candidate
encounters a demonstrated capability gap. The resulting task-local tool must
not gain project-wide authority. Fusion failure must not automatically
synthesize a tool; many failures require a code correction or a different
approach.

Completion criterion: a speculative refactor discovers a missing
transformation utility, creates and validates it inside the candidate
environment, uses it to complete the refactor and retains independent
verification of the candidate.

### M-04 — Attribute skill outcomes to actual use

**Priority:** Essential for useful learning

Files: `kernel/kernel.py`, `knowledge/pipeline.py`, `skills/lifecycle.py`,
`skills/selector.py`

Separate these concepts:

- A skill was available in the task context.
- The agent explicitly used its procedure.
- The procedure contributed to an independently verified result.

Extend existing task evidence so explicit skill application references the
exact skill ID/version and relevant tool calls. Track successful, failed and
inconclusive applications separately. A completed task must not automatically
count as successful use of every injected skill.

A failed task should generate targeted refinement candidates only for skills
with evidence of relevant use. Partial completion should preserve useful
failure evidence without being classified as proof that the skill failed.

Completion criterion: inject two relevant skills, deliberately use one and
complete the task. Only the used skill acquires supported reuse evidence. If
the task fails for an unrelated reason, neither skill receives an automatic
behavioral-failure verdict.

### M-05 — Make reusable workflows discoverable during ordinary tasks

**Priority:** Important

Files: `context/retrieval.py`, `strategy.py`, `workflows/store.py`,
`capabilities/workflow.py`

Add a bounded workflow-discovery projection that evaluates the current task
against promoted workflows' descriptions, input schemas, prerequisites,
environment and validation evidence. Expose lightweight summaries first and
allow full definitions only when relevant. Before execution, validate that the
task supplies required inputs and that the workflow remains compatible with
its runtime environment.

Completion criterion: after a parameterized procedure is taught through
independent tasks and promoted, a third related task can discover and use it
without naming the workflow.

### M-06 — Resolve behavioral evidence before promoting learned skills

**Priority:** Important

Files: `skills/validator.py`, `skills/lifecycle.py`, `knowledge/pipeline.py`

Introduce durable evidence resolution at promotion. Verify that source tasks
exist, cited verification results belong to the relevant procedure and the
proposed applicability matches observed environments. Require additional
evidence when widening a skill from one project to general user scope.

A structurally valid skill with insufficient behavioral evidence may remain an
unverified draft, but must not acquire a misleading verified status.

Completion criterion: two equally well-formed skills, one with genuine
passing task evidence and one with nonexistent receipts, are distinguished by
promotion.

### M-07 — Introduce one bounded adaptive decision gate

**Priority:** Important

Files: `kernel/reasoning_loop.py`, `strategy.py`, `protocol/failure.py`,
`interpreter/triggering.py`

Extend the kernel's existing decision boundary with a compact typed
recovery-state projection distinguishing implementation repair, strategy
change, missing-capability creation, stale-dependency revalidation, authority
request and stopping.

These are admissible options supported by observed failure, not an automatic
cascade. Re-evaluate them after material state changes such as failed tests,
discovered dependencies, changed project indexes or successful repair.

Completion criterion: for unrelated controlled failure classes, Athena chooses
the appropriate existing mechanism instead of repeatedly attempting source
repair or generating unnecessary tools.

## Useful but secondary implementations

### Live skill-file synchronization

Add an inexpensive revision-aware refresh path from `SkillLoader` into the
running lifecycle catalog. Report same-version content conflicts and preserve
the current turn's skill snapshot. Database-promoted skills already have a
dynamic retrieval path; this specifically addresses file-backed additions and
edits.

### Bounded parallel speculative comparison

`fusion/comparison.py` currently evaluates proposals sequentially. Independent
candidates may execute concurrently only with separate workspaces, immutable
base identities and individual budgets. Selection and promotion remain
serialized. This is a latency improvement, not a prerequisite for adaptive
reasoning.

### Composed behavioral acceptance

`tests/e2e/test_capability_reliability.py` exercises the real kernel with a
deterministic provider but supplies controlled Fusion results rather than
running the full Fusion/Shadow/Reality path. Add a composed test for genuine
candidate execution, correction, verification and promotion, followed by a
small live-model evaluation on unfamiliar tasks.

## Implementation boundary to preserve

| Mechanism | Responsibility |
|---|---|
| Kernel | Selects the next action, decides whether improvement is worthwhile and manages the bounded recovery allowance. |
| Fusion | Creates and compares isolated candidate work and returns structured outcomes. |
| Synthesis | Creates, repairs and validates task-local tools and returns versioned proof. |
| Knowledge | Records attributable observations and proposes reusable procedures. |
| Skill/workflow lifecycle | Validates, versions, exposes and explicitly promotes reusable knowledge. |
| RealityCoordinator | Independently verifies and governs candidate changes before they become real. |

The immediate priorities are M-01 through M-04 and the workflow
generalization defect. Together they close the central gap between having
advanced capabilities and reliably combining them into a self-improving task
workflow.

M-05 through M-07 then make those capabilities easier to discover, safer to
promote and more appropriate to invoke. Parallelism should come after the
composed sequential behavior is proven; otherwise it risks making incomplete
feedback loops faster without making them more reliable.
