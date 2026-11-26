from types import SimpleNamespace

from athena.kernel.adaptive_gate import project_adaptive_decision
from athena.kernel.observation_support import is_nonrecoverable_recovery_failure
from athena.protocol.messages import CapabilityResultBlock


def test_projection_distinguishes_generated_repair_from_fusion_strategy_change():
    generated = project_adaptive_decision(
        SimpleNamespace(
            generated_recovery_pending=True,
            generated_recovery_records=[{"failure_class": "implementation_failure"}],
            generated_recovery_limit=2,
            generated_recovery_attempts=1,
            speculative_recovery_pending=False,
            speculative_recovery_records=[],
        )
    )
    assert generated["kind"] == "implementation_repair"
    assert generated["action"] == "synthesis.repair"
    assert generated["remaining_attempts"] == 1

    fusion = project_adaptive_decision(
        SimpleNamespace(
            generated_recovery_pending=False,
            generated_recovery_records=[],
            speculative_recovery_pending=True,
            speculative_failure_records=[{"branch_id": "branch-1"}],
            speculative_recovery_limit=1,
            speculative_recovery_attempts=0,
        )
    )
    assert fusion["kind"] == "strategy_change"
    assert fusion["action"] == "fusion.run_or_compare"


def test_projection_is_ordinary_reasoning_without_pending_recovery():
    record = project_adaptive_decision(SimpleNamespace())
    assert record == {
        "kind": "ordinary_reasoning",
        "action": "continue_primary_loop",
        "reason": "no typed recovery transition is pending",
        "remaining_attempts": 0,
        "evidence": {},
    }


def test_recovery_mechanism_rejects_repeated_failed_proposal():
    import asyncio
    from types import SimpleNamespace

    from athena.kernel.recovery_dispatch import RecoveryDispatchMechanism
    from athena.protocol.messages import CapabilityCallBlock

    async def noop(*args, **kwargs):
        return None

    mechanism = RecoveryDispatchMechanism(
        emit=noop,
        update_metadata=noop,
        candidate_payload=lambda call: (
            [call.arguments["proposal"]],
            str(call.arguments.get("changes_from_previous") or ""),
        ),
        failed_fingerprints=lambda records: {
            mechanism._fingerprint(record["failed_operation"]) for record in records
        },
    )
    state = SimpleNamespace(
        speculative_recovery_pending=True,
        speculative_recovery_attempts=0,
        speculative_recovery_limit=1,
        speculative_recovery_rejections=0,
        speculative_failure_records=[{"failed_operation": [{"capability_id": "fs"}]}],
    )
    call = CapabilityCallBlock(
        call_id="call-repeat",
        capability_id="fusion",
        arguments={
            "operation": "run",
            "proposal": [{"capability_id": "fs"}],
            "changes_from_previous": "Try the same operation again.",
        },
    )
    assert mechanism.validate_speculative_recovery(state, [call]) == (
        "speculative recovery proposal repeats the failed approach"
    )
    assert state.speculative_recovery_rejections == 1
    asyncio.run(noop())


def test_nonrecoverable_recovery_failures_do_not_request_more_fusion():
    for error in (
        "unknown capability: create_session",
        "batch_preflight_failed: repair_invalid repair outcome INVALID",
        "session pytest_repro is not owned by task task-1",
        "security-sensitive identity of runtime session shell_task cannot be changed",
    ):
        result = CapabilityResultBlock(
            call_id="call-1",
            capability_id="execute",
            ok=False,
            error=error,
        )
        assert is_nonrecoverable_recovery_failure(result) is True


def test_pending_recovery_rejects_no_action_and_repeated_repair():
    import asyncio
    from types import SimpleNamespace

    from athena.kernel.recovery_dispatch import RecoveryDispatchMechanism
    from athena.protocol.messages import CapabilityCallBlock

    async def noop(*args, **kwargs):
        return None

    mechanism = RecoveryDispatchMechanism(
        emit=noop,
        update_metadata=noop,
        candidate_payload=lambda call: None,
        failed_fingerprints=lambda records: set(),
    )
    state = SimpleNamespace(
        speculative_recovery_pending=True,
        speculative_recovery_attempts=0,
        speculative_recovery_limit=1,
        speculative_recovery_rejections=0,
        speculative_failure_records=[{"failed_operation": [{"capability_id": "fs"}]}],
        generated_recovery_pending=True,
        generated_recovery_records=[{"target_capability_id": "synth_buggy"}],
        generated_recovery_attempts=0,
        generated_recovery_limit=1,
        generated_recovery_original=None,
        generated_recovery_retried=False,
        generated_recovery_repair_fingerprints=[],
    )
    assert mechanism.validate_speculative_recovery(state, []) == (
        "speculative recovery requires a materially different fusion proposal"
    )
    task = SimpleNamespace(id="task-recovery")
    assert asyncio.run(mechanism.validate_generated_recovery(task, state, [])) == (
        "generated recovery requires a complete repair for the failed capability"
    )
    repair = CapabilityCallBlock(
        call_id="repair-1",
        capability_id="synthesis",
        arguments={
            "operation": "repair",
            "capability_id": "synth_buggy",
            "code": "def run(): return 1",
        },
    )
    assert asyncio.run(mechanism.validate_generated_recovery(task, state, [repair])) is None
    state.generated_recovery_pending = True
    state.generated_recovery_attempts = 0
    assert asyncio.run(mechanism.validate_generated_recovery(task, state, [repair])) == (
        "generated recovery proposal repeats the failed repair"
    )
