"""Scenario registry: named release-gate families bound to real evidence.

The stable-beta audit (items P1.29/P1.30) requires that the release gate be
expressed as *named scenario families* with *machine-readable* pass/fail
evidence, rather than an opaque pytest exit code.  This module is the
declarative mapping the runner (``scripts/scenarios``) executes; it contains
no test logic of its own.

Design rules
------------
* **Curated, not invented.**  Every scenario binds to *existing* pytest node
  IDs or an existing probe command.  No new feature tests are written here.
* **Honesty over coverage.**  A subsystem with no real tests is registered
  with ``status="MISSING"`` — the runner reports it as a gap.  A scenario is
  never recorded as passed unless its bound evidence actually ran and exited
  zero in this invocation.
* **Family shape.**  Each family needs at least two scenarios.  Where the
  subsystem genuinely lacks tests, the missing scenarios are registered
  explicitly so the gap is visible in the manifest.

Entry shape::

    Scenario(
        id="SYNTH-001",
        family="SYNTH",
        title="...",
        nodeids=(...),            # pytest node IDs (mutually exclusive-ish
                                  # with `probe`)
        probe=("scripts/render-demo", "capability_fabric"),  # argv, exit 0 = pass
        required=True,             # required + missing/failed => runner exits 1
        status="READY",           # or "MISSING" to declare a known gap
        notes="...",               # free-form audit context
    )

Families (audit-mandated)
-------------------------
===================  ====================================================
Family               Meaning
===================  ====================================================
``FUSE-*``          one AgentKernel/model/task/execution authority
``SYNTH-*``          generated machinery (construction/validation/registration/reuse)
``AUTH-*``           generated-capability authority enforcement/policy containment
``COMPAT-*``         provider compatibility, replay, normalization, tool repair, caching
``BODY-*``           PTY, runtime, process, debugger, machine behavior
``WORLD-*``          TaskWorldState projection correctness
``CLAIM-*``          claim/evidence binding and staleness
``TX-*``             shadow execution, commit/discard, effect truthfulness
``REALITY-*``        exact-base commit, mutation CAS, retained recovery state
``FORK-*``           causal task branching
``ENV-*``            environment discovery and project adaptation
``RECOVERY-*``       hard-kill/restart truthfulness
``PROJECTION-*``     consistent CLI/OI/raw/body/API views
``VHS-*``            deterministic demo rendering and artifact validation
===================  ====================================================
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Scenario:
    """One named release-gate scenario bound to real, runnable evidence."""

    id: str
    family: str
    title: str
    status: str = "READY"  # READY | MISSING
    nodeids: tuple[str, ...] = ()
    probe: tuple[str, ...] = ()  # argv run from repo root; exit 0 == passed
    required: bool = True
    notes: str = ""

    def __post_init__(self) -> None:
        if self.status == "MISSING":
            # A declared gap must not carry evidence that could fake a pass.
            if self.nodeids or self.probe:
                raise ValueError(f"{self.id}: MISSING scenarios must not bind evidence")
        elif not self.nodeids and not self.probe:
            raise ValueError(f"{self.id}: READY scenarios need nodeids or probe")


# ---------------------------------------------------------------------------
# FUSE — one AgentKernel/model/task/execution authority
# ---------------------------------------------------------------------------
FUSE = (
    Scenario(
        id="FUSE-001",
        family="FUSE",
        title="Single ModelRouter construction; kernel receives injected router",
        nodeids=(
            "tests/unit/models/test_role_router.py::test_router_exposes_selected_provider_without_second_authority",
        ),
        required=False,  # companion scenario FUSE-002 documents the open defect
        notes=(
            "RoleRouter exposes the selected provider without building a second "
            "authority. The single sanctioned ModelRouter construction site is "
            "src/athena/service/service.py; see FUSE-002 and scripts/architecture-lint."
        ),
    ),
    Scenario(
        id="FUSE-002",
        family="FUSE",
        title="No kernel-constructed ModelRouter fallback",
        nodeids=(
            "tests/unit/models/test_router_authority.py::test_kernel_without_router_is_construction_error",
            "tests/unit/models/test_router_authority.py::test_kernel_holds_the_injected_router_instance",
        ),
        notes=(
            "FIXED (was P1-23 residual): the kernel REQUIRES an injected "
            "ModelRouter — no `router or ModelRouter(registry)` fallback "
            "exists. scripts/architecture-lint rule kernel-router-construction "
            "is green; these tests pin the contract."
        ),
    ),
    Scenario(
        id="FUSE-003",
        family="FUSE",
        title="Direct execution routes through the dispatcher and records audit",
        nodeids=(
            "tests/unit/service/test_direct_execution.py::test_direct_execution_routes_through_dispatcher_and_records_audit",
        ),
        notes="Direct shell path uses the same dispatch/audit authority as the kernel loop.",
    ),
    Scenario(
        id="FUSE-004",
        family="FUSE",
        title="Kernel loop drives model+capabilities to completion under one authority",
        nodeids=(
            "tests/unit/kernel/test_kernel_loop.py::test_end_to_end_simple_completes",
            "tests/unit/kernel/test_kernel_loop.py::test_scripted_capability_then_answer_runs_two_iterations",
        ),
    ),
    Scenario(
        id="FUSE-005",
        family="FUSE",
        title="Durable workflow resume reuses completed receipts and rejects drift",
        nodeids=(
            "tests/unit/workflows/test_executor.py::test_workflow_resume_is_bound_to_definition_inputs_and_owner",
            "tests/unit/workflows/test_executor.py::test_workflow_inflight_step_requires_recovery_instead_of_redispatch",
            "tests/unit/workflows/test_executor.py::test_workflow_reconciles_applied_receipt_without_redispatch",
            "tests/unit/workflows/test_executor.py::test_workflow_item_receipt_is_normalized_and_call_indexed",
            "tests/unit/workflows/test_executor.py::test_same_process_approval_replay_keeps_workflow_identity",
        ),
        notes=(
            "Workflow composition remains a dispatcher-bound execution path: "
            "completed call receipts are replayed, immutable run identity is "
            "checked, and an in-flight effect fails closed for recovery."
        ),
    ),
    Scenario(
        id="FUSE-006",
        family="FUSE",
        title="Verified external outcomes resume workflows without redispatch",
        nodeids=(
            "tests/unit/workflows/test_executor.py::test_external_failure_keeps_workflow_item_recoverable",
            "tests/unit/workflows/test_executor.py::test_verified_external_receipt_resumes_workflow_without_redispatch",
            "tests/unit/capabilities/test_workflow_promotion.py::test_workflow_recovery_uses_durable_external_receipt",
        ),
        notes=(
            "An unknown external result remains APPLYING until a durable receipt "
            "is verified; recovery then resumes the exact workflow item without "
            "issuing the external mutation again."
        ),
    ),
)

# ---------------------------------------------------------------------------
# SYNTH — generated machinery (construction, validation, registration, reuse)
# ---------------------------------------------------------------------------
SYNTH = (
    Scenario(
        id="SYNTH-001",
        family="SYNTH",
        title="Generated capabilities validate against fixtures before admission",
        nodeids=(
            "tests/unit/synthesis/test_synthesis.py::test_validate_passes_good_capability",
            "tests/unit/synthesis/test_synthesis.py::test_validate_catches_failing_case",
            "tests/unit/synthesis/test_synthesis.py::test_validate_reports_invalid_generated_schema_as_admission_failure",
        ),
    ),
    Scenario(
        id="SYNTH-002",
        family="SYNTH",
        title="Registration refuses unvalidated generated code; validated code runs",
        nodeids=(
            "tests/unit/synthesis/test_synthesis.py::test_register_ephemeral_refuses_unvalidated",
            "tests/unit/synthesis/test_synthesis.py::test_register_ephemeral_registers_validated_and_invokes",
        ),
    ),
    Scenario(
        id="SYNTH-003",
        family="SYNTH",
        title="Generated authority derives from sandbox profile, not declared effects",
        nodeids=(
            "tests/unit/synthesis/test_synthesis.py::test_generated_authority_is_sandbox_profile_not_declared_effects",
        ),
    ),
    Scenario(
        id="SYNTH-004",
        family="SYNTH",
        title="Generated machinery reuse: proof stats and skill promotion after two uses",
        nodeids=(
            "tests/unit/synthesis/test_synthesis.py::test_proof_for_returns_usage_stats",
            "tests/unit/synthesis/test_synthesis.py::test_to_skill_candidate_requires_diverse_repeated_success",
        ),
    ),
    Scenario(
        id="SYNTH-005",
        family="SYNTH",
        title="Model can synthesize a task-local tool with strict generated schema",
        nodeids=(
            "tests/unit/capabilities/test_synthesis_capability.py::test_model_can_create_task_local_tool",
            "tests/unit/capabilities/test_synthesis_capability.py::test_synthesis_generates_strict_input_schema_from_fixtures",
            "tests/unit/capabilities/test_synthesis_capability.py::test_synthesis_output_schema_inference_distinguishes_booleans",
        ),
    ),
    Scenario(
        id="SYNTH-006",
        family="SYNTH",
        title="Generated tools compose declared native capabilities through the dispatcher",
        nodeids=(
            "tests/unit/synthesis/test_synthesis.py::test_generated_host_calls_reenter_dispatcher_from_child",
            "tests/unit/synthesis/test_synthesis.py::test_generated_host_can_use_declared_native_write_ceiling",
            "tests/unit/synthesis/test_synthesis.py::test_generated_validation_rejects_undeclared_native_write",
        ),
        notes=(
            "Generated Python remains sandboxed; athena.call crosses a framed "
            "parent boundary and is re-authorized by the canonical dispatcher."
        ),
    ),
    Scenario(
        id="SYNTH-007",
        family="SYNTH",
        title="Successful workflow execution feeds the governed learning lifecycle",
        nodeids=(
            "tests/unit/knowledge/test_pipeline.py::test_successful_workflow_execution_enters_same_learning_store",
            "tests/unit/knowledge/test_pipeline.py::test_repeated_workflow_observations_merge_through_learning_store",
            "tests/unit/knowledge/test_pipeline.py::test_procedure_learning_excludes_capsule_transport_calls",
            "tests/unit/knowledge/test_pipeline.py::test_partial_task_does_not_create_workflow_candidate",
            "tests/unit/knowledge/test_pipeline.py::test_recovery_workflow_does_not_enter_learning_store",
            "tests/unit/capabilities/test_workflow_promotion.py::test_failed_workflow_does_not_notify_learning_observer",
            "tests/unit/capabilities/test_workflow_promotion.py::test_workflow_promotion_requires_distinct_observations",
        ),
        notes=(
            "Completed executions become reviewable workflow candidates with "
            "verification provenance; recovery and single-observation candidates "
            "cannot become reusable automation."
        ),
    ),
)

# ---------------------------------------------------------------------------
# AUTH — generated-capability authority enforcement / policy containment
# ---------------------------------------------------------------------------
AUTH = (
    Scenario(
        id="AUTH-001",
        family="AUTH",
        title="Generated-capability promotion is policy checked and task scoped",
        nodeids=(
            "tests/unit/capabilities/test_synthesis_capability.py::test_synthesis_promotion_is_policy_checked",
            "tests/unit/capabilities/test_synthesis_capability.py::test_synthesis_promotion_requires_diverse_live_proof",
            "tests/unit/capabilities/test_synthesis_capability.py::test_synthesis_requires_task_scope",
        ),
    ),
    Scenario(
        id="AUTH-002",
        family="AUTH",
        title="Restored generated tools re-check source and authority against profile",
        nodeids=(
            "tests/unit/affordances/test_fabric.py::test_restore_executor_rechecks_source_and_output_contract",
            "tests/unit/affordances/test_fabric.py::test_restore_executor_rejects_persisted_source_escape",
            "tests/unit/affordances/test_fabric.py::test_restore_executor_rejects_persisted_authority_outside_profile",
        ),
    ),
    Scenario(
        id="AUTH-003",
        family="AUTH",
        title="Scratch/generated execution rejects host escape before running",
        nodeids=(
            "tests/unit/capabilities/test_scratch.py::test_scratch_rejects_host_escape_before_execution",
            "tests/unit/affordances/test_validation.py::test_source_validation_rejects_host_escape_primitives_before_execution",
        ),
    ),
    Scenario(
        id="AUTH-004",
        family="AUTH",
        title="Task deny is a hard ceiling for capability policy containment",
        nodeids=(
            "tests/security/test_policy_bypass.py::test_task_deny_is_hard_ceiling_even_when_global_allows",
            "tests/contract/test_capability_policy_enforcement.py::TestPolicyEnforcement::test_task_deny_never_calls_executor",
        ),
    ),
)

# ---------------------------------------------------------------------------
# COMPAT — provider compatibility, replay, normalization, tool repair, caching
# ---------------------------------------------------------------------------
COMPAT = (
    Scenario(
        id="COMPAT-001",
        family="COMPAT",
        title="Provider streams translate and accumulate without losing content",
        nodeids=(
            "tests/unit/models/test_openai_compat.py::test_stream_events_delta_and_done_with_usage",
            "tests/unit/models/test_anthropic.py::test_anthropic_stream_accumulates_reasoning_text_and_tool_call",
            "tests/unit/models/test_response_accumulator.py::test_accumulator_merges_mixed_stream_without_duplicate_text",
            "tests/unit/models/test_response_accumulator.py::test_registry_invoke_uses_the_same_accumulator",
        ),
    ),
    Scenario(
        id="COMPAT-002",
        family="COMPAT",
        title="Assistant-turn replay preserves mixed text/tool history across providers",
        nodeids=(
            "tests/unit/kernel/test_model_tool_history.py::test_openai_and_anthropic_replay_preserve_mixed_assistant_turn",
            "tests/unit/kernel/test_model_tool_history.py::test_kernel_stream_assembly_keeps_text_and_tool_delta",
        ),
    ),
    Scenario(
        id="COMPAT-003",
        family="COMPAT",
        title="Deterministic tool-input repair fixes real malformations only",
        nodeids=(
            "tests/unit/models/test_compat_kernel.py::test_alias_repair_builtin",
            "tests/unit/models/test_compat_kernel.py::test_numeric_string_coercion",
            "tests/unit/models/test_compat_kernel.py::test_double_encoded_json",
            "tests/unit/models/test_compat_kernel.py::test_repair_is_idempotent",
        ),
    ),
    Scenario(
        id="COMPAT-004",
        family="COMPAT",
        title="Repair never invents data: unknown fields invalid, streams never repaired",
        nodeids=(
            "tests/unit/models/test_compat_kernel.py::test_unknown_required_field_is_invalid_not_invented",
            "tests/unit/models/test_compat_kernel.py::test_interrupted_stream_never_repaired",
            "tests/unit/models/test_toolcall_candidates.py::test_parse_never_manufactures_empty_dict",
            "tests/unit/models/test_toolcall_candidates.py::test_empty_candidate_is_not_rewritten_as_an_empty_object",
        ),
    ),
    Scenario(
        id="COMPAT-005",
        family="COMPAT",
        title="Prefix-cache telemetry is honest and boundary-marked",
        nodeids=(
            "tests/unit/models/test_compat_kernel.py::test_prefix_tracker_stable_then_boundary",
            "tests/unit/models/test_compat_kernel.py::test_prefix_tracker_marks_model_and_profile_changes_as_boundaries",
            "tests/unit/models/test_compat_kernel.py::test_usage_record_openai_cached_tokens",
            "tests/unit/models/test_compat_kernel.py::test_usage_record_anthropic_cache_write",
            "tests/unit/models/test_compat_kernel.py::test_no_cache_hit_inferred_without_telemetry",
        ),
    ),
    Scenario(
        id="COMPAT-006",
        family="COMPAT",
        title="Repaired arguments flow through dispatch preflight and are durable",
        nodeids=(
            "tests/unit/capabilities/test_dispatch_many_preflight.py::test_preflight_repaired_arguments_are_used",
            "tests/unit/capabilities/test_tool_repair_store.py::test_parallel_repair_receipt_is_durable_and_replayable",
        ),
    ),
)

# ---------------------------------------------------------------------------
# BODY — PTY, runtime, process, debugger, machine behavior
# ---------------------------------------------------------------------------
BODY = (
    Scenario(
        id="BODY-001",
        family="BODY",
        title="PTY terminal sessions: create, send, screen, kill with task ownership",
        nodeids=(
            "tests/unit/capabilities/test_terminal_session.py::test_create_send_screen_kill",
            "tests/unit/capabilities/test_terminal_session.py::test_task_ownership_enforced",
        ),
    ),
    Scenario(
        id="BODY-002",
        family="BODY",
        title="Terminal screen emulation reproduces rendered output",
        nodeids=(
            "tests/unit/capabilities/test_screen_emulator.py::test_overwrite_moves_cursor_and_replaces",
            "tests/unit/capabilities/test_screen_emulator.py::test_scrolling_keeps_last_rows",
            "tests/unit/capabilities/test_screen_emulator.py::test_alternate_screen_overwrite_via_cursor_reposition",
        ),
    ),
    Scenario(
        id="BODY-003",
        family="BODY",
        title="Real runtimes execute and keep state (shell, python, persistent session)",
        nodeids=(
            "tests/e2e/test_real_execution.py::test_shell_execution_runs_real_runtime",
            "tests/e2e/test_real_execution.py::test_python_execution_prints_4",
            "tests/e2e/test_real_execution.py::test_persistent_python_session_keeps_state",
            "tests/unit/execution/test_shell_runtime.py::test_shell_completion_marker_is_invocation_specific_and_exact",
        ),
    ),
    Scenario(
        id="BODY-004",
        family="BODY",
        title="Process identity and owned-process signal semantics",
        nodeids=(
            "tests/unit/execution/test_process_identity.py::test_owned_process_requires_matching_start_identity",
            "tests/unit/capabilities/test_system.py::test_process_signal_missing_pid",
            "tests/unit/capabilities/test_system.py::test_process_tree_and_usage",
        ),
    ),
    Scenario(
        id="BODY-005",
        family="BODY",
        title="Debugger capability uses governed DAP execution",
        nodeids=tuple(
            f"tests/unit/capabilities/test_debugger.py::{t}"
            for t in (
                "test_descriptor_tracks_optional_debugpy_installation",
                "test_launch_requires_execution_manager",
                "test_launch_and_breakpoint_use_governed_runtime",
                "test_close_all_is_safe_with_no_sessions",
                "test_session_ownership_is_enforced",
            )
        ),
        notes=(
            "FIXED: debugpy is optional, but when installed the capability "
            "launches the debuggee through ExecutionManager and owns a "
            "localhost DAP client. Workspace scope, task ownership, and "
            "loopback network policy remain enforced."
        ),
    ),
    Scenario(
        id="BODY-006",
        family="BODY",
        title="Machine introspection reports truthful, redacted environment",
        nodeids=(
            "tests/unit/capabilities/test_system.py::test_machine_overview",
            "tests/unit/capabilities/test_system.py::test_machine_env_redacts_secrets",
        ),
    ),
)

# ---------------------------------------------------------------------------
# WORLD — TaskWorldState projection correctness
# ---------------------------------------------------------------------------
WORLD = (
    Scenario(
        id="WORLD-001",
        family="WORLD",
        title="World-state registry projects claims, contradictions and unknowns",
        nodeids=(
            "tests/unit/shadow/test_shadow_and_worldstate.py::test_world_state_snapshot_shape",
            "tests/unit/shadow/test_shadow_and_worldstate.py::test_invariant_envelope_detects_violation",
        ),
    ),
    Scenario(
        id="WORLD-002",
        family="WORLD",
        title="World state survives restart and stays per-task scoped",
        nodeids=(
            "tests/unit/worldstate/test_store.py::test_persistence_across_restart",
            "tests/unit/worldstate/test_store.py::test_registry_without_store_is_in_memory",
            "tests/unit/worldstate/test_store.py::test_mutations_get_durable_task_sequences",
        ),
    ),
    Scenario(
        id="WORLD-003",
        family="WORLD",
        title="Invalidation flips persist and mark claims contradicted",
        nodeids=(
            "tests/unit/worldstate/test_store.py::test_invalidation_flip_persisted_and_scoped",
            "tests/unit/worldstate/test_store.py::test_mark_contradicted",
        ),
    ),
)

# ---------------------------------------------------------------------------
# CLAIM — claim/evidence binding and staleness
# ---------------------------------------------------------------------------
CLAIM = (
    Scenario(
        id="CLAIM-001",
        family="CLAIM",
        title="Claims go stale after a dependent mutation",
        nodeids=(
            "tests/unit/shadow/test_shadow_and_worldstate.py::test_claims_go_stale_after_dependent_mutation",
        ),
    ),
    Scenario(
        id="CLAIM-002",
        family="CLAIM",
        title="Experiment commit binds claims and invalidates stale ones",
        nodeids=(
            "tests/unit/fusion/test_orchestrator.py::test_experiment_commit_binds_claim_and_invalidates",
        ),
    ),
    Scenario(
        id="CLAIM-003",
        family="CLAIM",
        title="Claim leases are not consumed until execution finishes",
        nodeids=(
            "tests/unit/kernel/test_continuation_store.py::test_claim_is_not_consumed_until_execution_finishes",
            "tests/unit/kernel/test_continuation_store.py::test_restart_releases_claims_and_lists_recoverable_tasks",
        ),
    ),
)

# ---------------------------------------------------------------------------
# TX — shadow execution, commit/discard, effect truthfulness
# ---------------------------------------------------------------------------
TX = (
    Scenario(
        id="TX-001",
        family="TX",
        title="Shadow commit applies proven changes only",
        nodeids=(
            "tests/unit/shadow/test_shadow_and_worldstate.py::test_shadow_commit_applies_proven_changes",
        ),
    ),
    Scenario(
        id="TX-002",
        family="TX",
        title="Shadow discard leaves reality untouched; commit requires verification",
        nodeids=(
            "tests/unit/shadow/test_shadow_and_worldstate.py::test_shadow_discard_leaves_reality_untouched",
            "tests/unit/shadow/test_shadow_and_worldstate.py::test_commit_requires_verified_branch",
        ),
    ),
    Scenario(
        id="TX-003",
        family="TX",
        title="Shadow diffs detect same-size content edits and conflicts",
        nodeids=(
            "tests/unit/shadow/test_shadow_manifest.py::test_shadow_diff_detects_same_size_content_edit",
            "tests/unit/shadow/test_shadow_manifest.py::test_shadow_conflict_rejects_same_size_concurrent_edit",
        ),
    ),
    Scenario(
        id="TX-004",
        family="TX",
        title="Effect contracts are truthful: routing, WAL-before-effect, reversibility",
        nodeids=(
            "tests/unit/capabilities/test_effect_routing.py::test_exec_capabilities_resolve_to_execute_effects",
            "tests/unit/capabilities/test_effect_routing.py::test_policy_routes_terminal_session_to_execute_not_write",
            "tests/unit/capabilities/test_fs_mutations.py::test_write_ahead_intent_precedes_side_effect",
            "tests/unit/capabilities/test_fs_mutations.py::test_delete_snapshot_failure_is_not_reversible",
        ),
    ),
    Scenario(
        id="TX-005",
        family="TX",
        title="Failed/invalidated experiments are discarded, never committed",
        nodeids=(
            "tests/unit/fusion/test_orchestrator.py::test_failed_criteria_discards_and_auto_forks",
            "tests/unit/fusion/test_orchestrator.py::test_invariant_violation_blocks_commit",
        ),
    ),
    Scenario(
        id="TX-006",
        family="TX",
        title="Service control uses durable prepare/apply/verify/compensate receipts",
        nodeids=(
            "tests/unit/capabilities/test_environment.py::test_service_external_transaction_is_idempotent_verifiable_and_reversible",
            "tests/unit/capabilities/test_environment.py::test_reconstructed_service_capability_refuses_apply_after_crash",
            "tests/unit/capabilities/test_effect_routing.py::test_service_transaction_phases_have_exact_external_effect_contracts",
        ),
        notes=(
            "Systemd is outside the filesystem shadow. The explicit transaction "
            "protocol records identity, prevents duplicate apply, verifies state, "
            "and requires the prepared inverse for compensation."
        ),
    ),
    Scenario(
        id="TX-007",
        family="TX",
        title="Database writes use durable idempotent reversible receipts",
        nodeids=(
            "tests/unit/capabilities/test_environment.py::test_database_external_transaction_is_idempotent_verifiable_and_reversible",
            "tests/unit/capabilities/test_environment.py::test_reconstructed_database_capability_refuses_apply_after_crash",
            "tests/unit/capabilities/test_environment.py::test_database_external_apply_is_approval_gated_by_canonical_dispatcher",
            "tests/unit/capabilities/test_effect_routing.py::test_database_transaction_phases_have_exact_external_effect_contracts",
        ),
        notes=(
            "SQLite commits outside the shadow workspace are represented by a "
            "pre-image receipt, stable idempotency key, verification hash/query, "
            "and compensation restore."
        ),
    ),
    Scenario(
        id="TX-008",
        family="TX",
        title="HTTP external effects distinguish apply, recovery, and compensation proof",
        nodeids=(
            "tests/unit/capabilities/test_environment.py::test_http_external_transaction_has_receipts_idempotency_and_compensation",
            "tests/unit/capabilities/test_environment.py::test_http_external_transaction_marks_uncertain_apply_recovery",
            "tests/unit/capabilities/test_environment.py::test_http_external_transaction_rejects_non_success_apply",
            "tests/unit/capabilities/test_environment.py::test_reconstructed_http_capability_refuses_apply_after_crash",
        ),
        notes=(
            "A remote response is not treated as generic success: unknown apply "
            "outcomes require verification, and compensation remains SENT until "
            "a separate verification produces COMPENSATION_VERIFIED."
        ),
    ),
)

# ---------------------------------------------------------------------------
# REALITY — reality-bound commit and restart integrity
# ---------------------------------------------------------------------------
REALITY = (
    Scenario(
        id="REALITY-001",
        family="REALITY",
        title="Reality classification is deterministic and explainable",
        nodeids=(
            "tests/unit/reality/test_beta_contracts.py::test_reality_classifier_is_deterministic_and_explainable",
        ),
    ),
    Scenario(
        id="REALITY-002",
        family="REALITY",
        title="Verified candidate completion commits through the canonical path",
        nodeids=(
            "tests/unit/capabilities/test_reality_coordinator.py::test_simple_patch_reaches_real_workspace_only_after_verification",
            "tests/unit/capabilities/test_reality_integrity.py::test_internal_preimage_cas_refuses_stale_write",
        ),
    ),
    Scenario(
        id="REALITY-003",
        family="REALITY",
        title="Verification failure cannot complete or leak a candidate",
        nodeids=(
            "tests/unit/capabilities/test_reality_coordinator.py::test_unverified_candidate_yields_partial_not_complete",
        ),
    ),
    Scenario(
        id="REALITY-004",
        family="REALITY",
        title="Stale verification certificates fail closed",
        nodeids=(
            "tests/unit/shadow/test_commit_discard.py::test_commit_through_ask_profile_suspends_instead_of_auto_approving",
        ),
    ),
    Scenario(
        id="REALITY-005",
        family="REALITY",
        title="Complete-base drift retains the candidate and preserves reality",
        nodeids=(
            "tests/unit/shadow/test_commit_discard.py::test_commit_returns_conflict_when_reality_drifted",
        ),
    ),
    Scenario(
        id="REALITY-006",
        family="REALITY",
        title="Active candidate branches rehydrate after restart",
        nodeids=(
            "tests/unit/shadow/test_shadow_manifest.py::test_shadow_branch_metadata_survives_engine_restart",
        ),
    ),
    Scenario(
        id="REALITY-007",
        family="REALITY",
        title="Interrupted commits become explicit recovery state",
        nodeids=(
            "tests/unit/shadow/test_shadow_manifest.py::test_interrupted_commit_reconciles_without_guessing_outcome",
        ),
    ),
    Scenario(
        id="REALITY-008",
        family="REALITY",
        title="Restart refuses finalization after proven workspace drift",
        nodeids=(
            "tests/unit/reality/test_beta_contracts.py::test_restart_completion_refuses_final_workspace_drift",
        ),
    ),
    Scenario(
        id="REALITY-009",
        family="REALITY",
        title="Transactional compensation restores the owned checkpoint",
        nodeids=(
            "tests/unit/capabilities/test_reality_gate_escalation.py::test_transactional_checkpoints_real_workspace_and_compensates",
        ),
    ),
    Scenario(
        id="REALITY-010",
        family="REALITY",
        title="Transactional compensation refuses unrelated drift",
        nodeids=(
            "tests/unit/capabilities/test_reality_integrity.py::test_transaction_compensation_refuses_unrelated_drift_after_owned_write",
        ),
    ),
    Scenario(
        id="REALITY-011",
        family="REALITY",
        title="Rollback refuses to overwrite externally changed reality",
        nodeids=(
            "tests/unit/capabilities/test_reality_integrity.py::test_rollback_refuses_to_overwrite_external_edit",
        ),
    ),
    Scenario(
        id="REALITY-012",
        family="REALITY",
        title="Unsupported resource types are rejected before commit",
        nodeids=(
            "tests/unit/shadow/test_commit_discard.py::test_commit_retains_and_reports_unsupported_symlink",
        ),
    ),
    Scenario(
        id="REALITY-013",
        family="REALITY",
        title="Opaque execution is isolated from the real workspace",
        nodeids=(
            "tests/unit/capabilities/test_reality_gate.py::test_opaque_execution_cannot_write_the_real_workspace",
        ),
    ),
    Scenario(
        id="REALITY-014",
        family="REALITY",
        title="Transactional verification uses compare-and-swap ownership",
        nodeids=(
            "tests/unit/capabilities/test_reality_integrity.py::test_transactional_verification_uses_cas_against_verified_revision",
        ),
    ),
    Scenario(
        id="REALITY-015",
        family="REALITY",
        title="Completion recovery fails closed without final reality identity",
        nodeids=(
            "tests/unit/capabilities/test_reality_integrity.py::test_completion_recovery_requires_fingerprint_and_final_identity",
        ),
    ),
)

# ---------------------------------------------------------------------------
# FORK — causal task branching
# ---------------------------------------------------------------------------
FORK = (
    Scenario(
        id="FORK-001",
        family="FORK",
        title="Fork creates a new task with causal metadata; unknown task rejected",
        nodeids=(
            "tests/unit/causal/test_fork_checkpoint.py::test_fork_creates_new_task_with_metadata",
            "tests/unit/causal/test_fork_checkpoint.py::test_fork_unknown_task_raises",
        ),
    ),
    Scenario(
        id="FORK-002",
        family="FORK",
        title="Checkpoint capture/restore with concurrent-change rejection",
        nodeids=(
            "tests/unit/causal/test_fork_checkpoint.py::test_checkpoint_capture_and_restore",
            "tests/unit/causal/test_fork_checkpoint.py::test_checkpoint_restore_rejects_concurrent_workspace_change",
            "tests/unit/causal/test_fork_checkpoint.py::test_checkpoint_materialize_creates_independent_workspace",
        ),
    ),
    Scenario(
        id="FORK-003",
        family="FORK",
        title="Fusion forks from events with checkpoints and speculative cleanup",
        nodeids=(
            "tests/unit/fusion/test_orchestrator.py::test_fork_from_event_with_checkpoint",
            "tests/unit/causal/test_fork_checkpoint.py::test_fork_task_creation_failure_removes_speculative_session",
        ),
    ),
)

# ---------------------------------------------------------------------------
# ENV — environment discovery and project adaptation
# ---------------------------------------------------------------------------
ENV = (
    Scenario(
        id="ENV-001",
        family="ENV",
        title="Environment capability gates database access on workspace context",
        nodeids=(
            "tests/unit/capabilities/test_environment.py::test_database_requires_workspace_context",
            "tests/unit/capabilities/test_environment.py::test_database_read_connection_cannot_attach_or_mutate",
        ),
    ),
    Scenario(
        id="ENV-002",
        family="ENV",
        title="Project adaptation: skills discovered from environment are injected",
        nodeids=(
            "tests/integration/test_skill_injection.py::test_skill_injected_when_trigger_in_objective",
        ),
    ),
    Scenario(
        id="ENV-003",
        family="ENV",
        title="Watch capability observes filesystem/process reality truthfully",
        nodeids=(
            "tests/unit/capabilities/test_watch.py::test_file_watch_detects_same_size_edit_with_preserved_timestamp",
            "tests/unit/capabilities/test_watch.py::test_process_exit_observation_is_one_shot_and_truthful",
        ),
    ),
)

# ---------------------------------------------------------------------------
# RECOVERY — hard-kill/restart truthfulness
# ---------------------------------------------------------------------------
RECOVERY = (
    Scenario(
        id="RECOVERY-001",
        family="RECOVERY",
        title="Running tasks recover after hard kill; parallelism is real",
        nodeids=(
            "tests/crash/test_hard_crash_recovery.py::test_running_task_recovered_after_hard_crash",
            "tests/crash/test_hard_crash_recovery.py::test_worker_parallelism_real",
        ),
    ),
    Scenario(
        id="RECOVERY-002",
        family="RECOVERY",
        title="Task states reconcile truthfully across restart",
        nodeids=(
            "tests/crash/test_task_recovery.py::test_running_task_becomes_interrupted_on_hard_stop",
            "tests/crash/test_task_recovery.py::test_queued_task_survives_restart",
            "tests/crash/test_task_recovery.py::test_mutation_intent_wal_survives_crash",
        ),
    ),
    Scenario(
        id="RECOVERY-003",
        family="RECOVERY",
        title="Scheduler occurrences reconcile on restart; no double fire",
        nodeids=(
            "tests/crash/test_scheduler_recovery.py::test_claimed_occurrence_reconciled_to_fired_on_restart",
            "tests/crash/test_scheduler_recovery.py::test_job_not_fired_before_stop_fires_after_restart",
        ),
    ),
    Scenario(
        id="RECOVERY-004",
        family="RECOVERY",
        title="External effects fail closed on restart and remain explicitly recoverable",
        nodeids=(
            "tests/unit/state/test_external_effects.py::test_startup_reconciles_inflight_external_effect",
            "tests/unit/state/test_external_effects.py::test_prepared_external_effect_remains_actionable_after_startup_reconcile",
            "tests/unit/state/test_external_effects.py::test_recovery_provenance_survives_durable_restart",
        ),
    ),
)

# ---------------------------------------------------------------------------
# PROJECTION — consistent CLI/OI/raw/body/API views
# ---------------------------------------------------------------------------
PROJECTION = (
    Scenario(
        id="PROJECTION-001",
        family="PROJECTION",
        title="OI viewer renders events to configured output, never process stdout",
        nodeids=(
            "tests/unit/cli/test_oi_stream.py::test_oi_viewer_renders_to_configured_output_and_keeps_partials",
            "tests/unit/cli/test_oi_stream.py::test_oi_viewer_does_not_write_to_process_stdout",
        ),
    ),
    Scenario(
        id="PROJECTION-002",
        family="PROJECTION",
        title="CLI surface groups code/runtime output and shares the execution surface",
        nodeids=(
            "tests/unit/cli/test_surface.py::test_surface_groups_code_and_runtime_output",
            "tests/unit/cli/test_surface.py::test_direct_shell_escape_uses_the_same_execution_surface",
        ),
    ),
    Scenario(
        id="PROJECTION-003",
        family="PROJECTION",
        title="Operator API views project consistent read-only snapshots",
        nodeids=(
            "tests/unit/service/test_operator_views.py::test_operator_permissions_empty",
            "tests/unit/service/test_operator_views.py::test_operator_diff_empty",
            "tests/unit/service/test_operator_views.py::test_operator_artifacts_empty",
        ),
    ),
)

# ---------------------------------------------------------------------------
# VHS — deterministic demo rendering and artifact validation
# ---------------------------------------------------------------------------
VHS = (
    Scenario(
        id="VHS-001",
        family="VHS",
        title="Demo render publishes a validated gif or a documented failure code",
        probe=("scripts/render-demo", "capability_fabric"),
        required=False,
        notes=(
            "Probe contract (scripts/render-demo header): 0 success; 2 tool/"
            "argument error (e.g. vhs missing — treated as 'skipped', not a "
            "failure); 3 timeout; 4 vhs failure; 5 artifact validation failure. "
            "Artifact-existence alone is never accepted as completion. "
            "FIXED (2026-08-26): the width/height check previously read inside "
            "the ASCII 'GIF89a' header (bytes 2-3/3-4) instead of the Logical "
            "Screen Descriptor (bytes 6-9), and the frame count used `-c copy` "
            "which never decodes frames. Both repaired: dimensions read from "
            "offsets 6-7/8-9, frames decoded with the LAST frame=N progress "
            "value. Probe validates end-to-end: 1280x720, a readable ~90s "
            "recording, and a non-zero decoded frame count."
        ),
    ),
    Scenario(
        id="VHS-002",
        family="VHS",
        title="Demo driver consumes the generated-capability fabric fixtures",
        nodeids=(
            "tests/unit/capabilities/test_synthesis_capability.py::test_synthesis_create_uses_canonical_dispatcher",
        ),
        notes=(
            "demos/capability_fabric_demo.py drives the same capability-fabric "
            "path (fixtures in demos/fixtures/capability_fabric.jsonl) that this "
            "bound test pins; the tape renders that driver."
        ),
        required=False,
    ),
    Scenario(
        id="VHS-003",
        family="VHS",
        title="GIF artifact validation itself is byte-correct",
        nodeids=(
            "tests/unit/scripts/test_render_demo_validation.py::test_logical_screen_descriptor_offsets_are_correct",
            "tests/unit/scripts/test_render_demo_validation.py::test_ascii_header_bytes_are_not_dimensions",
            "tests/unit/scripts/test_render_demo_validation.py::test_render_demo_script_parses_known_good_dimensions",
            "tests/unit/scripts/test_render_demo_validation.py::test_published_demo_gif_has_correct_header",
        ),
        notes=(
            "FIXED (was declared gap): render-demo's header parsing is pinned "
            "at the byte level — Logical Screen Descriptor offsets 6-9 are "
            "the dimensions, the ASCII-header read that caused VHS-001's "
            "14406x14648 failure is asserted to be wrong, and the exact od "
            "pipeline the script runs is executed against synthetic bytes."
        ),
        required=False,
    ),
)

SCENARIOS: tuple[Scenario, ...] = (
    *FUSE,
    *SYNTH,
    *AUTH,
    *COMPAT,
    *BODY,
    *WORLD,
    *CLAIM,
    *TX,
    *FORK,
    *ENV,
    *RECOVERY,
    *PROJECTION,
    *VHS,
    *REALITY,
)

FAMILY_ORDER: tuple[str, ...] = (
    "FUSE",
    "SYNTH",
    "AUTH",
    "COMPAT",
    "BODY",
    "WORLD",
    "CLAIM",
    "TX",
    "FORK",
    "ENV",
    "RECOVERY",
    "PROJECTION",
    "VHS",
    "REALITY",
)

FAMILY_DESCRIPTIONS: dict[str, str] = {
    "FUSE": "one AgentKernel/model/task/execution authority",
    "SYNTH": "generated machinery (construction, validation, registration, reuse)",
    "AUTH": "generated-capability authority enforcement and policy containment",
    "COMPAT": "provider compatibility, replay, normalization, tool repair, caching",
    "BODY": "PTY, runtime, process, debugger, machine behavior",
    "WORLD": "TaskWorldState projection correctness",
    "CLAIM": "claim/evidence binding and staleness",
    "TX": "shadow execution, commit/discard, effect truthfulness",
    "FORK": "causal task branching",
    "ENV": "environment discovery and project adaptation",
    "RECOVERY": "hard-kill/restart truthfulness",
    "PROJECTION": "consistent CLI/OI/raw/body/API views",
    "VHS": "deterministic demo rendering and artifact validation",
    "REALITY": "exact-base commit, mutation CAS, retained recovery state",
}

# Audit-mandated families that have no registered scenarios at all would be a
# registry bug (each family must carry >= 2 scenarios, real or declared gaps).
_expected = set(FAMILY_ORDER)
_registered = {s.family for s in SCENARIOS}
_missing_families = _expected - _registered
assert not _missing_families, f"families without scenarios: {_missing_families}"
_ids = [s.id for s in SCENARIOS]
assert len(_ids) == len(set(_ids)), "duplicate scenario id"


def by_family(family: str) -> tuple[Scenario, ...]:
    """Return the scenarios of one family (FAMILY_ORDER member)."""
    return tuple(s for s in SCENARIOS if s.family == family)


def scenario(scenario_id: str) -> Scenario:
    """Look up a scenario by ID; KeyError with the known IDs on miss."""
    for s in SCENARIOS:
        if s.id == scenario_id:
            return s
    raise KeyError(f"unknown scenario {scenario_id!r}; known: {_ids}")
