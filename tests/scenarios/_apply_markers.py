"""One-shot helper (run by the scenarios slice) to attach athena_scenario markers.

Inserts `@pytest.mark.athena_scenario("FAM-00N")` above bound test functions.
Metadata only; no test logic is touched.  Kept for auditability of exactly
which lines the slice added to existing test files.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

MARKS = {
    ("tests/unit/models/test_role_router.py", "test_router_exposes_selected_provider_without_second_authority"): ["FUSE-001"],
    ("tests/unit/service/test_direct_execution.py", "test_direct_execution_routes_through_dispatcher_and_records_audit"): ["FUSE-003"],
    ("tests/unit/kernel/test_kernel_loop.py", "test_end_to_end_simple_completes"): ["FUSE-004"],
    ("tests/unit/kernel/test_kernel_loop.py", "test_scripted_capability_then_answer_runs_two_iterations"): ["FUSE-004"],
    ("tests/unit/synthesis/test_synthesis.py", "test_validate_passes_good_capability"): ["SYNTH-001"],
    ("tests/unit/synthesis/test_synthesis.py", "test_validate_catches_failing_case"): ["SYNTH-001"],
    ("tests/unit/synthesis/test_synthesis.py", "test_validate_reports_invalid_generated_schema_as_admission_failure"): ["SYNTH-001"],
    ("tests/unit/synthesis/test_synthesis.py", "test_register_ephemeral_refuses_unvalidated"): ["SYNTH-002"],
    ("tests/unit/synthesis/test_synthesis.py", "test_register_ephemeral_registers_validated_and_invokes"): ["SYNTH-002"],
    ("tests/unit/synthesis/test_synthesis.py", "test_generated_authority_is_sandbox_profile_not_declared_effects"): ["SYNTH-003"],
    ("tests/unit/synthesis/test_synthesis.py", "test_proof_for_returns_usage_stats"): ["SYNTH-004"],
    ("tests/unit/synthesis/test_synthesis.py", "test_to_skill_candidate_requires_two_uses"): ["SYNTH-004"],
    ("tests/unit/capabilities/test_synthesis_capability.py", "test_model_can_create_task_local_tool"): ["SYNTH-005"],
    ("tests/unit/capabilities/test_synthesis_capability.py", "test_synthesis_generates_strict_input_schema_from_fixtures"): ["SYNTH-005"],
    ("tests/unit/capabilities/test_synthesis_capability.py", "test_synthesis_output_schema_inference_distinguishes_booleans"): ["SYNTH-005"],
    ("tests/unit/capabilities/test_synthesis_capability.py", "test_synthesis_promotion_is_policy_checked"): ["AUTH-001"],
    ("tests/unit/capabilities/test_synthesis_capability.py", "test_synthesis_requires_task_scope"): ["AUTH-001"],
    ("tests/unit/affordances/test_fabric.py", "test_restore_executor_rechecks_source_and_output_contract"): ["AUTH-002"],
    ("tests/unit/affordances/test_fabric.py", "test_restore_executor_rejects_persisted_source_escape"): ["AUTH-002"],
    ("tests/unit/affordances/test_fabric.py", "test_restore_executor_rejects_persisted_authority_outside_profile"): ["AUTH-002"],
    ("tests/unit/capabilities/test_scratch.py", "test_scratch_rejects_host_escape_before_execution"): ["AUTH-003"],
    ("tests/unit/affordances/test_validation.py", "test_source_validation_rejects_host_escape_primitives_before_execution"): ["AUTH-003"],
    ("tests/security/test_policy_bypass.py", "test_task_deny_is_hard_ceiling_even_when_global_allows"): ["AUTH-004"],
    ("tests/unit/models/test_compat_kernel.py", "test_alias_repair_builtin"): ["COMPAT-003"],
    ("tests/unit/models/test_compat_kernel.py", "test_numeric_string_coercion"): ["COMPAT-003"],
    ("tests/unit/models/test_compat_kernel.py", "test_double_encoded_json"): ["COMPAT-003"],
    ("tests/unit/models/test_compat_kernel.py", "test_repair_is_idempotent"): ["COMPAT-003"],
    ("tests/unit/models/test_compat_kernel.py", "test_unknown_required_field_is_invalid_not_invented"): ["COMPAT-004"],
    ("tests/unit/models/test_compat_kernel.py", "test_interrupted_stream_never_repaired"): ["COMPAT-004"],
    ("tests/unit/models/test_toolcall_candidates.py", "test_parse_never_manufactures_empty_dict"): ["COMPAT-004"],
    ("tests/unit/models/test_toolcall_candidates.py", "test_empty_candidate_is_not_rewritten_as_an_empty_object"): ["COMPAT-004"],
    ("tests/unit/models/test_compat_kernel.py", "test_prefix_tracker_stable_then_boundary"): ["COMPAT-005"],
    ("tests/unit/models/test_compat_kernel.py", "test_usage_record_openai_cached_tokens"): ["COMPAT-005"],
    ("tests/unit/models/test_compat_kernel.py", "test_no_cache_hit_inferred_without_telemetry"): ["COMPAT-005"],
    ("tests/unit/capabilities/test_dispatch_many_preflight.py", "test_preflight_repaired_arguments_are_used"): ["COMPAT-006"],
    ("tests/unit/capabilities/test_tool_repair_store.py", "test_parallel_repair_receipt_is_durable_and_replayable"): ["COMPAT-006"],
    ("tests/unit/kernel/test_model_tool_history.py", "test_openai_and_anthropic_replay_preserve_mixed_assistant_turn"): ["COMPAT-002"],
    ("tests/unit/models/test_response_accumulator.py", "test_accumulator_merges_mixed_stream_without_duplicate_text"): ["COMPAT-001"],
    ("tests/unit/capabilities/test_terminal_session.py", "test_create_send_screen_kill"): ["BODY-001"],
    ("tests/unit/capabilities/test_terminal_session.py", "test_task_ownership_enforced"): ["BODY-001"],
    ("tests/unit/capabilities/test_screen_emulator.py", "test_overwrite_moves_cursor_and_replaces"): ["BODY-002"],
    ("tests/unit/capabilities/test_screen_emulator.py", "test_scrolling_keeps_last_rows"): ["BODY-002"],
    ("tests/unit/capabilities/test_system.py", "test_machine_overview"): ["BODY-006"],
    ("tests/unit/capabilities/test_system.py", "test_machine_env_redacts_secrets"): ["BODY-006"],
    ("tests/unit/shadow/test_shadow_and_worldstate.py", "test_world_state_snapshot_shape"): ["WORLD-001"],
    ("tests/unit/shadow/test_shadow_and_worldstate.py", "test_claims_go_stale_after_dependent_mutation"): ["CLAIM-001"],
    ("tests/unit/shadow/test_shadow_and_worldstate.py", "test_shadow_commit_applies_proven_changes"): ["TX-001"],
    ("tests/unit/shadow/test_shadow_and_worldstate.py", "test_shadow_discard_leaves_reality_untouched"): ["TX-002"],
    ("tests/unit/fusion/test_orchestrator.py", "test_experiment_commit_binds_claim_and_invalidates"): ["CLAIM-002"],
    ("tests/unit/fusion/test_orchestrator.py", "test_failed_criteria_discards_and_auto_forks"): ["TX-005"],
    ("tests/unit/kernel/test_continuation_store.py", "test_claim_is_not_consumed_until_execution_finishes"): ["CLAIM-003"],
    ("tests/unit/causal/test_fork_checkpoint.py", "test_fork_creates_new_task_with_metadata"): ["FORK-001"],
    ("tests/unit/causal/test_fork_checkpoint.py", "test_checkpoint_capture_and_restore"): ["FORK-002"],
    ("tests/unit/capabilities/test_environment.py", "test_database_requires_workspace_context"): ["ENV-001"],
    ("tests/unit/capabilities/test_watch.py", "test_file_watch_detects_same_size_edit_with_preserved_timestamp"): ["ENV-003"],
    ("tests/crash/test_hard_crash_recovery.py", "test_running_task_recovered_after_hard_crash"): ["RECOVERY-001"],
    ("tests/crash/test_task_recovery.py", "test_running_task_becomes_interrupted_on_hard_stop"): ["RECOVERY-002"],
    ("tests/crash/test_scheduler_recovery.py", "test_claimed_occurrence_reconciled_to_fired_on_restart"): ["RECOVERY-003"],
    ("tests/unit/cli/test_oi_stream.py", "test_oi_viewer_renders_to_configured_output_and_keeps_partials"): ["PROJECTION-001"],
    ("tests/unit/cli/test_oi_stream.py", "test_oi_viewer_does_not_write_to_process_stdout"): ["PROJECTION-001"],
    ("tests/unit/cli/test_surface.py", "test_direct_shell_escape_uses_the_same_execution_surface"): ["PROJECTION-002"],
}


def main() -> None:
    by_file: dict[str, dict[str, list[str]]] = {}
    for (f, t), ids in MARKS.items():
        by_file.setdefault(f, {})[t] = ids

    changed = []
    for rel, marks in sorted(by_file.items()):
        path = ROOT / rel
        text = path.read_text()
        marker = "@pytest.mark.athena_scenario("
        if "import pytest" not in text:
            # add the import the marker needs; nothing else changes
            if "from __future__ import annotations\n" in text:
                text = text.replace(
                    "from __future__ import annotations\n",
                    "from __future__ import annotations\n\nimport pytest", 1)
            else:
                text = "import pytest\n\n" + text
        lines = text.split("\n")
        out = []
        inserted = set()
        for idx, line in enumerate(lines):
            m = re.match(r"^(async def|def)\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", line)
            if m and m.group(2) in marks and m.group(2) not in inserted:
                # idempotent: skip if the previous non-empty line is already
                # an athena_scenario marker
                prev = next(
                    (lines[j] for j in range(idx - 1, -1, -1) if lines[j].strip()),
                    "")
                if "athena_scenario" in prev:
                    inserted.add(m.group(2))
                else:
                    ids = marks[m.group(2)]
                    out.append(marker + ", ".join(f'"{i}"' for i in ids) + ")")
                    inserted.add(m.group(2))
            out.append(line)
        missed = set(marks) - inserted
        if missed:
            raise SystemExit(f"{rel}: could not locate {missed}")
        new = "\n".join(out)
        if new != text:
            path.write_text(new)
            changed.append(f"{rel} (+{len(inserted)} markers)")
    for c in changed:
        print(c)
    print(f"{len(changed)} files marked")


if __name__ == "__main__":
    main()
