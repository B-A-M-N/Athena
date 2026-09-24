from types import SimpleNamespace

from athena.kernel.adaptive_gate import project_adaptive_decision


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
