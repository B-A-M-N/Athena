from __future__ import annotations

import json

import pytest

from athena.affordances import CapabilityFabric, GeneratedCapabilityStore
from athena.affordances.models import AffordanceScope, GeneratedCapability
from athena.capabilities.dispatcher import CapabilityDispatcher
from athena.capabilities.registry import CapabilityRegistry
from athena.capabilities.synthesis import SynthesisCapability
from athena.policy.engine import PolicyEngine
from athena.protocol.capabilities import (
    CapabilityRequest,
    CapabilityRequestOrigin,
    CapabilityResultStatus,
)
from athena.protocol.tasks import AutonomyLevel, WorkspaceSpec
from athena.state.database import Database
from athena.synthesis.engine import SynthesisEngine


@pytest.mark.asyncio
@pytest.mark.athena_scenario("SYNTH-005")
async def test_model_can_create_task_local_tool(tmp_path):
    fabric = CapabilityFabric(CapabilityRegistry())
    capability = SynthesisCapability(SynthesisEngine(), fabric)
    request = CapabilityRequest(
        capability_id="synthesis",
        task_id="task-1",
        call_id="create-1",
        origin=CapabilityRequestOrigin.MODEL,
        arguments={
            "operation": "create",
            "name": "echo_helper",
            "description": "Echo a message as structured output",
            "code": "def run(args):\n    return {'echo': args['msg']}\n",
            "input_schema": {
                "type": "object",
                "required": ["msg"],
                "properties": {"msg": {"type": "string"}},
                "additionalProperties": False,
            },
            "output_schema": {
                "type": "object",
                "required": ["echo"],
                "properties": {"echo": {"type": "string"}},
            },
            "effects": ["READ_LOCAL"],
            "validation_cases": [{"args": {"msg": "hello"}}],
        },
    )

    result = await capability.invoke(request)

    assert result.status is CapabilityResultStatus.OK
    created = json.loads(result.output)
    generated_id = created["capability_id"]
    assert fabric.has(generated_id, task_id="task-1")
    assert not fabric.has(generated_id, task_id="task-2")
    assert created["proof"]["validation"]["all_passed"] is True

    executor = fabric.executor_for(generated_id, task_id="task-1")
    context = type(
        "Context",
        (),
        {
            "workspace": WorkspaceSpec(id="repo", root=str(tmp_path)),
        },
    )()
    for index, message in enumerate(("world", "athena", "runtime"), start=1):
        invocation = await executor.invoke(
            CapabilityRequest(
                capability_id=generated_id,
                arguments={"msg": message},
                task_id="task-1",
                call_id=f"call-{index}",
                session_id=f"session-{index % 2}",
            ),
            context=context,
        )
        assert invocation.status is CapabilityResultStatus.OK
        assert json.loads(invocation.output) == {"echo": message}

    promoted = await capability.invoke(
        CapabilityRequest(
            capability_id="synthesis",
            task_id="task-1",
            call_id="promote-1",
            arguments={
                "operation": "promote",
                "capability_id": generated_id,
                "scope": "project",
            },
        ),
        context=type(
            "Context",
            (),
            {
                "workspace": WorkspaceSpec(id="repo", root=str(tmp_path)),
            },
        )(),
    )
    assert promoted.status is CapabilityResultStatus.OK
    assert fabric.has(generated_id, project_id="repo")
    assert not fabric.has(generated_id, task_id="task-1")


@pytest.mark.asyncio
async def test_model_can_declare_composed_capability_ceiling():
    fabric = CapabilityFabric(CapabilityRegistry())
    capability = SynthesisCapability(SynthesisEngine(), fabric)
    result = await capability.invoke(
        CapabilityRequest(
            capability_id="synthesis",
            task_id="task-composed",
            call_id="create-composed",
            origin=CapabilityRequestOrigin.MODEL,
            arguments={
                "operation": "create",
                "name": "declared_composer",
                "description": "A generated tool with a declared host-call ceiling",
                "code": "def run(args):\n    return {'ok': True}\n",
                "input_schema": {"type": "object", "additionalProperties": False},
                "effects": ["READ_LOCAL"],
                "required_capabilities": ["fs", "execute"],
                "validation_cases": [{"args": {}}],
            },
        )
    )

    assert result.status is CapabilityResultStatus.OK, result.error
    generated = json.loads(result.output)
    assert generated["proof"]["required_capabilities"] == ["execute", "fs"]


@pytest.mark.asyncio
async def test_model_selected_persistent_runtime_survives_rehydration():
    fabric = CapabilityFabric(CapabilityRegistry())
    engine = SynthesisEngine()
    capability = SynthesisCapability(engine, fabric)
    result = await capability.invoke(
        CapabilityRequest(
            capability_id="synthesis",
            task_id="task-persistent-record",
            call_id="create-persistent-record",
            origin=CapabilityRequestOrigin.MODEL,
            arguments={
                "operation": "create",
                "name": "persistent_record",
                "description": "keeps task-local generated state",
                "runtime": "python_persistent",
                "code": "def run(args):\n    return {'ok': True}\n",
                "input_schema": {"type": "object"},
                "validation_cases": [{"args": {}}],
            },
        )
    )

    assert result.status is CapabilityResultStatus.OK, result.error
    capability_id = json.loads(result.output)["capability_id"]
    assert json.loads(result.output)["proof"]["runtime"] == "python_persistent"
    generated = fabric._records[capability_id]
    assert generated.runtime == "python_persistent"

    restored_record = GeneratedCapability.from_record(generated.to_record())
    restored_engine = SynthesisEngine()
    restored_engine.restore_executor(restored_record)
    assert restored_engine.synthetic_for(capability_id).runtime == "python_persistent"


@pytest.mark.asyncio
async def test_synthesis_repair_creates_provenanced_superseding_candidate():
    fabric = CapabilityFabric(CapabilityRegistry())
    capability = SynthesisCapability(SynthesisEngine(), fabric)
    created = await capability.invoke(
        CapabilityRequest(
            capability_id="synthesis",
            task_id="task-repair",
            call_id="create-repair",
            arguments={
                "operation": "create",
                "name": "repairable_helper",
                "description": "A helper whose contract can change",
                "code": "def run(args):\n    return {'value': args['value']}\n",
                "input_schema": {"type": "object"},
                "effects": ["READ_LOCAL"],
                "validation_cases": [{"args": {"value": "old"}}],
            },
        )
    )
    target_id = json.loads(created.output)["capability_id"]

    repaired = await capability.invoke(
        CapabilityRequest(
            capability_id="synthesis",
            task_id="task-repair",
            call_id="repair-1",
            arguments={
                "operation": "repair",
                "capability_id": target_id,
                "name": "repairable_helper_v2",
                "description": "A repaired helper with the new output contract",
                "code": "def run(args):\n    return {'value': args['value'].upper()}\n",
                "input_schema": {"type": "object"},
                "effects": ["READ_LOCAL"],
                "validation_cases": [{"args": {"value": "new"}}],
            },
        )
    )

    assert repaired.status is CapabilityResultStatus.OK, repaired.error
    repaired_payload = json.loads(repaired.output)
    repaired_id = repaired_payload["capability_id"]
    assert repaired_id != target_id
    assert repaired_payload["proof"]["supersedes"] == [target_id]
    assert fabric.has(repaired_id, task_id="task-repair")
    assert fabric.has(target_id, task_id="task-repair")


@pytest.mark.asyncio
@pytest.mark.athena_scenario("AUTH-001")
async def test_synthesis_promotion_is_policy_checked(tmp_path):
    registry = CapabilityRegistry()
    fabric = CapabilityFabric(registry)
    engine = SynthesisEngine()
    capability = SynthesisCapability(engine, fabric)
    registry.register(capability)
    dispatcher = CapabilityDispatcher(
        registry,
        PolicyEngine(AutonomyLevel.CODING),
        fabric=fabric,
    )
    create = await capability.invoke(
        CapabilityRequest(
            capability_id="synthesis",
            task_id="task-4",
            call_id="create-4",
            arguments={
                "operation": "create",
                "name": "promotable_helper",
                "description": "A helper for promotion testing",
                "code": "def run(args):\n    return {'ok': True}\n",
                "input_schema": {"type": "object"},
                "effects": ["READ_LOCAL"],
                "validation_cases": [{"args": {}}],
            },
        )
    )
    generated_id = json.loads(create.output)["capability_id"]
    executor = fabric.executor_for(generated_id, task_id="task-4")
    for index in range(3):
        live = await executor.invoke(
            CapabilityRequest(
                capability_id=generated_id,
                task_id="task-4",
                call_id=f"promotion-proof-{index}",
                session_id=f"promotion-session-{index % 2}",
                arguments={"value": index},
            )
        )
        assert live.status is CapabilityResultStatus.OK
    result = await dispatcher.dispatch(
        CapabilityRequest(
            capability_id="synthesis",
            task_id="task-4",
            call_id="promote-4",
            origin=CapabilityRequestOrigin.MODEL,
            arguments={
                "operation": "promote",
                "capability_id": generated_id,
                "scope": "project",
            },
        ),
        workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
    )
    assert result.status is CapabilityResultStatus.OK
    assert fabric.has(generated_id, project_id="repo")


@pytest.mark.asyncio
async def test_engine_promotion_cannot_bypass_target_tier_validation(tmp_path):
    fabric = CapabilityFabric(CapabilityRegistry())
    engine = SynthesisEngine()
    capability = SynthesisCapability(engine, fabric)
    created = await capability.invoke(
        CapabilityRequest(
            capability_id="synthesis",
            task_id="task-direct-promote",
            call_id="create-direct-promote",
            arguments={
                "operation": "create",
                "name": "direct_promote_helper",
                "description": "requires target-tier validation",
                "code": "def run(args):\n    return {'ok': True}\n",
                "input_schema": {"type": "object"},
                "validation_cases": [{"args": {}}],
            },
        )
    )
    generated_id = json.loads(created.output)["capability_id"]
    executor = fabric.executor_for(generated_id, task_id="task-direct-promote")
    for index in range(3):
        live = await executor.invoke(
            CapabilityRequest(
                capability_id=generated_id,
                task_id="task-direct-promote",
                session_id=f"session-{index % 2}",
                call_id=f"direct-proof-{index}",
                arguments={"value": index},
            ),
            context=type(
                "Context",
                (),
                {
                    "workspace": WorkspaceSpec(id="repo", root=str(tmp_path)),
                },
            )(),
        )
        assert live.status is CapabilityResultStatus.OK

    assert not engine.promote(
        fabric,
        generated_id,
        scope=AffordanceScope.PROJECT,
        project_id="repo",
    )
    assert not fabric.has(generated_id, project_id="repo")


@pytest.mark.asyncio
@pytest.mark.athena_scenario("AUTH-001")
async def test_synthesis_promotion_requires_diverse_live_proof(tmp_path):
    fabric = CapabilityFabric(CapabilityRegistry())
    capability = SynthesisCapability(SynthesisEngine(), fabric)
    created = await capability.invoke(
        CapabilityRequest(
            capability_id="synthesis",
            task_id="task-proof-gate",
            call_id="create-proof-gate",
            arguments={
                "operation": "create",
                "name": "proof_gate_helper",
                "description": "cannot be promoted from fixture proof alone",
                "code": "def run(args):\n    return {'ok': True}\n",
                "input_schema": {"type": "object"},
                "validation_cases": [{"args": {}}],
            },
        )
    )
    generated_id = json.loads(created.output)["capability_id"]

    result = await capability.invoke(
        CapabilityRequest(
            capability_id="synthesis",
            task_id="task-proof-gate",
            call_id="promote-proof-gate",
            arguments={
                "operation": "promote",
                "capability_id": generated_id,
                "scope": "project",
            },
        ),
        context=type(
            "Context",
            (),
            {
                "workspace": WorkspaceSpec(id="repo", root=str(tmp_path)),
            },
        )(),
    )

    assert result.status is CapabilityResultStatus.FAILED
    assert "live promotion proof" in (result.error or "")
    assert not fabric.has(generated_id, project_id="repo")


@pytest.mark.asyncio
@pytest.mark.athena_scenario("AUTH-001")
async def test_synthesis_requires_task_scope(tmp_path):
    capability = SynthesisCapability(SynthesisEngine(), CapabilityFabric(CapabilityRegistry()))
    result = await capability.invoke(
        CapabilityRequest(
            capability_id="synthesis",
            task_id=None,
            call_id="create-2",
            arguments={"operation": "create"},
        )
    )
    assert result.status is CapabilityResultStatus.FAILED
    assert "task scope" in (result.error or "")


@pytest.mark.asyncio
async def test_synthesis_create_uses_canonical_dispatcher(tmp_path):
    registry = CapabilityRegistry()
    fabric = CapabilityFabric(registry)
    registry.register(SynthesisCapability(SynthesisEngine(), fabric))
    dispatcher = CapabilityDispatcher(
        registry,
        PolicyEngine(AutonomyLevel.CODING),
        fabric=fabric,
    )
    result = await dispatcher.dispatch(
        CapabilityRequest(
            capability_id="synthesis",
            task_id="task-3",
            call_id="create-3",
            origin=CapabilityRequestOrigin.MODEL,
            arguments={
                "operation": "create",
                "name": "canonical_helper",
                "description": "A dispatcher-admitted helper",
                "code": "def run(args):\n    return {'ok': True}\n",
                "input_schema": {"type": "object"},
                "effects": ["READ_LOCAL"],
                "validation_cases": [{"args": {}}],
            },
        ),
        workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
    )
    assert result.status is CapabilityResultStatus.OK
    generated_id = json.loads(result.output)["capability_id"]
    assert fabric.has(generated_id, task_id="task-3")


@pytest.mark.asyncio
async def test_task_generated_capability_can_be_explicitly_deprecated():
    fabric = CapabilityFabric(CapabilityRegistry())
    capability = SynthesisCapability(SynthesisEngine(), fabric)
    created = await capability.invoke(
        CapabilityRequest(
            capability_id="synthesis",
            task_id="task-deprecate",
            call_id="create-deprecate",
            arguments={
                "operation": "create",
                "name": "retirable_helper",
                "description": "A helper with an explicit lifecycle",
                "code": "def run(args):\n    return {'ok': True}\n",
                "input_schema": {"type": "object"},
                "effects": ["READ_LOCAL"],
                "validation_cases": [{"args": {}}],
            },
        )
    )
    generated_id = json.loads(created.output)["capability_id"]
    assert fabric.has(generated_id, task_id="task-deprecate")

    retired = await capability.invoke(
        CapabilityRequest(
            capability_id="synthesis",
            task_id="task-deprecate",
            call_id="deprecate",
            arguments={"operation": "deprecate", "capability_id": generated_id},
        )
    )

    assert retired.status is CapabilityResultStatus.OK
    assert not fabric.has(generated_id, task_id="task-deprecate")


@pytest.mark.asyncio
@pytest.mark.athena_scenario("SYNTH-005")
async def test_synthesis_generates_strict_input_schema_from_fixtures():
    fabric = CapabilityFabric(CapabilityRegistry())
    capability = SynthesisCapability(SynthesisEngine(), fabric)
    result = await capability.invoke(
        CapabilityRequest(
            capability_id="synthesis",
            task_id="task-schema",
            call_id="create-schema",
            arguments={
                "operation": "create",
                "name": "fixture_contract",
                "description": "A helper whose contract is inferred from fixtures",
                "code": "def run(args):\n    return {'path': args['path']}\n",
                "validation_cases": [
                    {"args": {"path": "one.txt"}},
                    {"args": {"path": "two.txt"}},
                ],
            },
        )
    )

    assert result.status is CapabilityResultStatus.OK
    generated_id = json.loads(result.output)["capability_id"]
    executor = fabric.executor_for(generated_id, task_id="task-schema")
    assert executor.descriptor.input_schema == {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    }
    assert executor.descriptor.output_schema == {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    }


@pytest.mark.athena_scenario("SYNTH-005")
def test_synthesis_output_schema_inference_distinguishes_booleans():
    from athena.synthesis.engine import _schema_for_values

    assert _schema_for_values([{"ok": True}, {"ok": False}]) == {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }


@pytest.mark.asyncio
async def test_promoted_capability_usage_proof_survives_restart(tmp_path):
    db = Database(str(tmp_path / "proof.db"))
    store = GeneratedCapabilityStore(db)
    fabric = CapabilityFabric(CapabilityRegistry(), store=store)
    engine = SynthesisEngine()
    capability = SynthesisCapability(engine, fabric)

    created = await capability.invoke(
        CapabilityRequest(
            capability_id="synthesis",
            task_id="task-proof",
            call_id="create-proof",
            arguments={
                "operation": "create",
                "name": "proof_helper",
                "description": "A helper whose live proof is durable",
                "code": "def run(args):\n    return {'ok': args['ok']}\n",
                "input_schema": {
                    "type": "object",
                    "required": ["ok"],
                    "properties": {"ok": {"type": "boolean"}},
                    "additionalProperties": False,
                },
                "validation_cases": [{"args": {"ok": True}}],
            },
        )
    )
    generated_id = json.loads(created.output)["capability_id"]
    context = type(
        "Context",
        (),
        {
            "workspace": WorkspaceSpec(id="repo", root=str(tmp_path)),
        },
    )()
    executor = fabric.executor_for(generated_id, task_id="task-proof")
    for index, value in enumerate((True, False, True)):
        live = await executor.invoke(
            CapabilityRequest(
                capability_id=generated_id,
                task_id="task-proof",
                call_id=f"proof-seed-{index}",
                session_id=f"proof-session-{index % 2}",
                arguments={"ok": value},
            ),
            context=context,
        )
        assert live.status is CapabilityResultStatus.OK
    promoted = await capability.invoke(
        CapabilityRequest(
            capability_id="synthesis",
            task_id="task-proof",
            call_id="promote-proof",
            arguments={
                "operation": "promote",
                "capability_id": generated_id,
                "scope": "project",
            },
        ),
        context=context,
    )
    assert promoted.status is CapabilityResultStatus.OK

    executor = fabric.executor_for(generated_id, project_id="repo")
    result = await executor.invoke(
        CapabilityRequest(
            capability_id=generated_id,
            task_id="task-proof",
            call_id="use-proof",
            arguments={"ok": False},
        ),
        context=context,
    )
    assert result.status is CapabilityResultStatus.OK

    loaded = await GeneratedCapabilityStore(db).get(
        generated_id,
        project_id="repo",
    )
    assert loaded is not None
    assert loaded.proof_record["usage"] == {
        "uses": 4,
        "successes": 4,
        "failures": 0,
    }
    await db.close()


@pytest.mark.asyncio
async def test_candidate_can_be_rehydrated_and_promoted_after_restart(tmp_path):
    db = Database(str(tmp_path / "candidate.db"))
    store = GeneratedCapabilityStore(db)

    registry = CapabilityRegistry()
    fabric = CapabilityFabric(registry, store=store)
    engine = SynthesisEngine()
    capability = SynthesisCapability(engine, fabric)
    created = await capability.invoke(
        CapabilityRequest(
            capability_id="synthesis",
            task_id="task-candidate",
            call_id="create-candidate",
            arguments={
                "operation": "create",
                "name": "candidate_helper",
                "description": "retains enough evidence to review after restart",
                "code": "def run(args):\n    return {'value': args['value']}\n",
                "input_schema": {
                    "type": "object",
                    "required": ["value"],
                    "properties": {"value": {"type": "integer"}},
                    "additionalProperties": False,
                },
                "validation_cases": [
                    {"args": {"value": 1}},
                    {"args": {"value": 2}},
                ],
            },
        )
    )
    capability_id = json.loads(created.output)["capability_id"]
    executor = fabric.executor_for(capability_id, task_id="task-candidate")
    for value in (10, 11, 12):
        result = await executor.invoke(
            CapabilityRequest(
                capability_id=capability_id,
                arguments={"value": value},
                task_id="task-candidate",
                call_id=f"candidate-use-{value}",
                session_id=f"candidate-session-{value % 2}",
            )
        )
        assert result.status is CapabilityResultStatus.OK
    await fabric.flush()
    candidate = await store.get(capability_id, task_id="task-candidate")
    assert candidate is not None
    assert candidate.scope is AffordanceScope.CANDIDATE
    assert len(candidate.validation_cases) == 2

    # A new engine/fabric has no process-local executor or synthetic record.
    restarted_fabric = CapabilityFabric(CapabilityRegistry(), store=store)
    restarted = SynthesisCapability(SynthesisEngine(), restarted_fabric)
    promoted = await restarted.invoke(
        CapabilityRequest(
            capability_id="synthesis",
            task_id="task-candidate",
            call_id="promote-candidate",
            arguments={
                "operation": "promote",
                "capability_id": capability_id,
                "scope": "project",
            },
        ),
        context=type(
            "Context",
            (),
            {
                "workspace": WorkspaceSpec(id="repo", root=str(tmp_path)),
            },
        )(),
    )
    assert promoted.status is CapabilityResultStatus.OK, promoted.error
    assert restarted_fabric.has(capability_id, project_id="repo")
    durable = await store.get(capability_id, project_id="repo")
    assert durable is not None
    assert durable.validation_state == "PROMOTED"
    assert durable.validation_cases
    await db.close()
