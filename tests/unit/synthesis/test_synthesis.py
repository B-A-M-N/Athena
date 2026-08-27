"""Unit tests for athena.synthesis.engine (SynthesisEngine)."""

from __future__ import annotations

import json

import pytest

from athena.capabilities.registry import CapabilityRegistry
from athena.affordances.models import DependencyRequirement, AffordanceScope
from athena.protocol.capabilities import (
    CapabilityRequest,
    CapabilityResultStatus,
    EffectClass,
)
from athena.protocol.errors import CapabilityUnavailable
from athena.synthesis.engine import SynthesisEngine

GOOD_CODE = "def run(args):\n    return {'echo': args.get('msg', '')}\n"
BAD_CODE = "def run(args):\n    raise RuntimeError('boom')\n"


def _make_cap(engine, code=GOOD_CODE, name="greeter"):
    return engine.synthesize(
        name=name,
        description="echoes a message",
        code=code,
        input_schema={"type": "object", "properties": {}},
        effects={"READ_LOCAL"},
        task_id="task_1",
    )


@pytest.mark.asyncio
@pytest.mark.athena_scenario("SYNTH-001")
async def test_validate_passes_good_capability():
    engine = SynthesisEngine()
    cap = _make_cap(engine)
    result = await engine.validate(cap, [{"args": {"msg": "hi"},
                                          "expect_output_contains": "hi"}])
    assert result.validation["all_passed"] is True
    assert result.validation["cases_passed"] == 1


@pytest.mark.asyncio
@pytest.mark.athena_scenario("SYNTH-001")
async def test_validate_catches_failing_case():
    engine = SynthesisEngine()
    cap = _make_cap(engine, code=BAD_CODE, name="broken")
    result = await engine.validate(cap, [{"args": {}}])
    assert result.validation["all_passed"] is False
    assert result.validation["cases_passed"] == 0


@pytest.mark.asyncio
@pytest.mark.athena_scenario("SYNTH-001")
async def test_validate_reports_invalid_generated_schema_as_admission_failure():
    engine = SynthesisEngine()
    cap = _make_cap(engine)
    cap.input_schema = {"type": "not-a-json-schema-type"}

    result = await engine.validate(cap, [{"args": {}}])

    assert result.validation["all_passed"] is False
    assert result.validation["details"][0]["case"] == "static"
    assert "static validation" in result.validation["details"][0]["error"]


@pytest.mark.athena_scenario("SYNTH-002")
def test_register_ephemeral_refuses_unvalidated():
    engine = SynthesisEngine()
    registry = CapabilityRegistry()
    cap = _make_cap(engine)  # never validated
    assert cap.validation.get("all_passed") is not True
    assert engine.register_ephemeral(registry, cap) is False
    with pytest.raises(CapabilityUnavailable):
        registry.resolve("synth_greeter")


@pytest.mark.athena_scenario("SYNTH-003")
def test_generated_authority_is_sandbox_profile_not_declared_effects():
    engine = SynthesisEngine()
    cap = engine.synthesize(
        name="restricted_helper",
        description="computes a value",
        code=GOOD_CODE,
        input_schema={"type": "object"},
        effects={"READ_LOCAL", "WRITE_LOCAL", "NETWORK_WRITE", "PRIVILEGED"},
    )

    assert cap.effects == frozenset({
        "READ_LOCAL", "WRITE_LOCAL", "NETWORK_WRITE", "PRIVILEGED",
    })
    assert cap.effective_effects == frozenset({
        EffectClass.READ_LOCAL.value, EffectClass.EXECUTE.value,
    })


def test_generated_record_carries_reproducible_dependency_lock():
    engine = SynthesisEngine()
    cap = engine.synthesize(
        name="locked_helper",
        description="uses an explicitly recorded dependency set",
        code=GOOD_CODE,
        required_dependencies=(
            DependencyRequirement("httpx", version="0.28.1", reason="fetch"),
        ),
    )

    generated = engine._generated_record(
        cap, scope=AffordanceScope.PROJECT, project_scope="repo"
    )
    lock = generated.dependency_lock
    assert lock["format"] == 1
    assert lock["requirements"][0]["name"] == "httpx"
    assert len(lock["fingerprint"]) == 64


@pytest.mark.asyncio
@pytest.mark.athena_scenario("SYNTH-002")
async def test_register_ephemeral_registers_validated_and_invokes():
    engine = SynthesisEngine()
    registry = CapabilityRegistry()
    cap = _make_cap(engine)
    await engine.validate(cap, [{"args": {"msg": "hi"}}])
    assert engine.register_ephemeral(registry, cap) is True
    descriptor = registry.resolve("synth_greeter")
    assert descriptor.id == "synth_greeter"

    executor = registry.executor_for("synth_greeter")
    request = CapabilityRequest(
        capability_id=cap.id, arguments={"msg": "hello"},
        task_id="task_1", call_id="call_1")
    result = await executor.invoke(request)
    assert result.status == CapabilityResultStatus.OK
    assert json.loads(result.output) == {"echo": "hello"}
    assert cap.uses == 1 and cap.successes == 1


@pytest.mark.asyncio
@pytest.mark.athena_scenario("SYNTH-004")
async def test_proof_for_returns_usage_stats():
    engine = SynthesisEngine()
    registry = CapabilityRegistry()
    cap = _make_cap(engine)
    await engine.validate(cap, [{"args": {}}])
    engine.register_ephemeral(registry, cap)

    assert engine.proof_for("nope") is None
    proof = engine.proof_for(cap.id)
    assert proof is not None
    assert proof["uses"] == 0
    assert proof["validation"]["all_passed"] is True
    assert proof["effects"] == ["READ_LOCAL"]

    executor = registry.executor_for(cap.id)
    for i in range(2):
        await executor.invoke(CapabilityRequest(
            capability_id=cap.id, arguments={}, task_id="task_1",
            call_id=f"c{i}"))
    proof = engine.proof_for(cap.id)
    assert proof["uses"] == 2
    assert proof["successes"] == 2


@pytest.mark.asyncio
@pytest.mark.athena_scenario("SYNTH-004")
async def test_to_skill_candidate_requires_two_uses():
    engine = SynthesisEngine()
    registry = CapabilityRegistry()
    cap = _make_cap(engine)
    await engine.validate(cap, [{"args": {}}])
    engine.register_ephemeral(registry, cap)

    assert engine.to_skill_candidate(cap.id) is None  # zero uses

    executor = registry.executor_for(cap.id)
    await executor.invoke(CapabilityRequest(
        capability_id=cap.id, arguments={}, task_id="task_1", call_id="c1"))
    assert engine.to_skill_candidate(cap.id) is None  # only one use

    await executor.invoke(CapabilityRequest(
        capability_id=cap.id, arguments={}, task_id="task_1", call_id="c2"))
    candidate = engine.to_skill_candidate(cap.id)
    assert candidate is not None
    assert candidate.draft.name == "greeter"
    assert len(candidate.evidence) == 2
