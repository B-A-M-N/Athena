from __future__ import annotations

import json
import os
from dataclasses import replace
from types import SimpleNamespace

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
from athena.protocol.events import make_event
from athena.protocol.tasks import AutonomyLevel, WorkspaceSpec
from athena.state.database import Database
from athena.synthesis.engine import SynthesisEngine


async def _record_passing_verifications(
    engine: SynthesisEngine,
    capability_id: str,
    task_id: str,
    call_ids: list[str],
) -> None:
    """Feed the same canonical proof events production observes."""
    for call_id in call_ids:
        await engine.observe_event(
            make_event(
                "CapabilityCompleted",
                {"call_id": call_id, "capability_id": capability_id},
                task_id=task_id,
            )
        )
        await engine.observe_event(
            make_event(
                "VerificationCompleted",
                {"passed": True},
                task_id=task_id,
            )
        )


@pytest.mark.asyncio
@pytest.mark.athena_scenario("SYNTH-005")
async def test_model_can_create_task_local_tool(tmp_path):
    fabric = CapabilityFabric(CapabilityRegistry())
    engine = SynthesisEngine()
    engine.bind_proof_sink(fabric.update_generated_proof)
    capability = SynthesisCapability(engine, fabric)
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
            "validation_cases": [{"args": {"msg": "hello"}, "expect_output": {"echo": "hello"}}],
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
    await _record_passing_verifications(
        engine,
        generated_id,
        "task-1",
        ["call-1", "call-2", "call-3"],
    )

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

    class _Registry:
        def resolve(self, capability_id):
            if capability_id in {"fs", "execute"}:
                return SimpleNamespace(availability=SimpleNamespace(value="available"))
            raise KeyError(capability_id)

    engine = SynthesisEngine(dispatcher=SimpleNamespace(registry=_Registry()))
    capability = SynthesisCapability(engine, fabric)
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
    assert repaired_payload["proof"]["validation"]["cases_total"] == 2
    assert repaired_payload["proof"]["validation"]["cases_passed"] == 2
    assert fabric.has(repaired_id, task_id="task-repair")
    assert not fabric.has(target_id, task_id="task-repair")


@pytest.mark.asyncio
async def test_synthesis_revalidate_restores_same_task_capability_identity(tmp_path):
    fabric = CapabilityFabric(CapabilityRegistry())
    engine = SynthesisEngine()
    capability = SynthesisCapability(engine, fabric)
    created = await capability.invoke(
        CapabilityRequest(
            capability_id="synthesis",
            task_id="task-revalidate",
            call_id="create-revalidate",
            arguments={
                "operation": "create",
                "name": "revalidatable_helper",
                "description": "a helper that can regain current proof",
                "code": "def run(args):\n    return {'ok': True}\n",
                "input_schema": {"type": "object", "additionalProperties": False},
                "output_schema": {
                    "type": "object",
                    "required": ["ok"],
                    "properties": {"ok": {"type": "boolean"}},
                    "additionalProperties": False,
                },
                "validation_cases": [{"args": {}}],
            },
        )
    )
    target_id = json.loads(created.output)["capability_id"]
    target_cap = engine.synthetic_for(target_id)
    assert target_cap is not None
    identity = (
        target_cap.id,
        target_cap.code,
        dict(target_cap.input_schema),
        dict(target_cap.output_schema or {}),
        target_cap.revision,
        target_cap.parent_revision,
        target_cap.family_id,
    )
    target_cap.lifecycle_state = "REVALIDATION_REQUIRED"
    fabric._records[target_id] = replace(
        fabric._records[target_id], lifecycle_state="REVALIDATION_REQUIRED"
    )

    result = await capability.invoke(
        CapabilityRequest(
            capability_id="synthesis",
            task_id="task-revalidate",
            call_id="revalidate-1",
            arguments={"operation": "revalidate", "capability_id": target_id},
        ),
        context=type(
            "Context",
            (),
            {"workspace": WorkspaceSpec(id="repo", root=str(tmp_path))},
        )(),
    )

    assert result.status is CapabilityResultStatus.OK, result.error
    assert json.loads(result.output)["status"] == "revalidated"
    restored = engine.synthetic_for(target_id)
    assert restored is not None
    assert (
        restored.id,
        restored.code,
        dict(restored.input_schema),
        dict(restored.output_schema or {}),
        restored.revision,
        restored.parent_revision,
        restored.family_id,
    ) == identity
    assert restored.lifecycle_state == "VALIDATED"
    assert fabric.has(target_id, task_id="task-revalidate")


@pytest.mark.asyncio
async def test_synthesis_migrate_contract_is_explicit_and_revisioned():
    fabric = CapabilityFabric(CapabilityRegistry())
    engine = SynthesisEngine()
    capability = SynthesisCapability(engine, fabric)
    created = await capability.invoke(
        CapabilityRequest(
            capability_id="synthesis",
            task_id="task-migrate",
            call_id="create-migrate",
            arguments={
                "operation": "create",
                "name": "contract_helper",
                "description": "a helper with a stable contract",
                "code": "def run(args):\n    return {'value': args['value']}\n",
                "input_schema": {"type": "object"},
                "validation_cases": [{"args": {"value": "old"}}],
            },
        )
    )
    target_id = json.loads(created.output)["capability_id"]
    predecessor = engine.synthetic_for(target_id)

    migrated = await capability.invoke(
        CapabilityRequest(
            capability_id="synthesis",
            task_id="task-migrate",
            call_id="migrate-1",
            arguments={
                "operation": "migrate_contract",
                "capability_id": target_id,
                "name": "contract_helper",
                "description": "a helper with a revised contract",
                "code": "def run(args):\n    return {'value': args['value'], 'version': 2}\n",
                "input_schema": {
                    "type": "object",
                    "required": ["value"],
                    "properties": {"value": {"type": "string"}},
                    "additionalProperties": False,
                },
                "output_schema": {
                    "type": "object",
                    "required": ["value", "version"],
                    "properties": {
                        "value": {"type": "string"},
                        "version": {"type": "integer"},
                    },
                    "additionalProperties": False,
                },
                "validation_cases": [{"args": {"value": "new"}}],
            },
        )
    )

    assert migrated.status is CapabilityResultStatus.OK, migrated.error
    payload = json.loads(migrated.output)
    successor = engine.synthetic_for(payload["capability_id"])
    assert successor is not None
    assert predecessor is not None
    assert successor.family_id == predecessor.family_id
    assert successor.revision == predecessor.revision + 1
    assert successor.parent_revision == predecessor.revision
    assert successor.supersedes == (target_id,)
    assert successor.input_schema != predecessor.input_schema
    migrated_inputs = [case["args"] for case in successor.validation_cases or []]
    assert {"value": "old"} in migrated_inputs
    assert {"value": "new"} in migrated_inputs


@pytest.mark.asyncio
async def test_breaking_contract_migration_requires_trusted_operator_confirmation():
    fabric = CapabilityFabric(CapabilityRegistry())
    engine = SynthesisEngine()
    capability = SynthesisCapability(engine, fabric)
    created = await capability.invoke(
        CapabilityRequest(
            capability_id="synthesis",
            task_id="task-breaking-migrate",
            call_id="create-breaking-migrate",
            arguments={
                "operation": "create",
                "name": "breaking_helper",
                "description": "a helper to migrate explicitly",
                "code": "def run(args):\n    return args['value']\n",
                "input_schema": {"type": "object"},
                "validation_cases": [{"args": {"value": "old"}}],
            },
        )
    )
    target_id = json.loads(created.output)["capability_id"]
    rejected = await capability.invoke(
        CapabilityRequest(
            capability_id="synthesis",
            task_id="task-breaking-migrate",
            call_id="breaking-migrate-1",
            arguments={
                "operation": "migrate_contract",
                "capability_id": target_id,
                "name": "breaking_helper",
                "description": "a breaking helper migration",
                "code": "def run(args):\n    return args['value']\n",
                "input_schema": {
                    "type": "object",
                    "required": ["value"],
                    "properties": {"value": {"type": "integer"}},
                },
                "validation_cases": [{"args": {"value": 1}}],
                "compatibility": "breaking",
                "operator_confirmation": True,
            },
        )
    )
    assert rejected.status is CapabilityResultStatus.FAILED
    assert "trusted caller" in (rejected.error or "")


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
                "validation_cases": [{"args": {}, "expect_output": {"ok": True}}],
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
    await _record_passing_verifications(
        engine,
        generated_id,
        "task-4",
        [f"promotion-proof-{index}" for index in range(3)],
    )
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

    assert not await engine.promote(
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
    engine.bind_proof_sink(fabric.update_generated_proof)
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
                "validation_cases": [{"args": {"ok": True}, "expect_output": {"ok": True}}],
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
    await _record_passing_verifications(
        engine,
        generated_id,
        "task-proof",
        [f"proof-seed-{index}" for index in range(3)],
    )
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
async def test_promotion_persistence_failure_keeps_task_overlay_live(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "promotion-failure.db"))
    store = GeneratedCapabilityStore(db)
    fabric = CapabilityFabric(CapabilityRegistry(), store=store)
    engine = SynthesisEngine()
    engine.bind_proof_sink(fabric.update_generated_proof)
    capability = SynthesisCapability(engine, fabric)
    created = await capability.invoke(
        CapabilityRequest(
            capability_id="synthesis",
            task_id="task-promotion-failure",
            call_id="create-promotion-failure",
            arguments={
                "operation": "create",
                "name": "promotion_failure_helper",
                "description": "a helper whose failed promotion must remain task-live",
                "code": "def run(args):\n    return {'ok': args['ok']}\n",
                "input_schema": {
                    "type": "object",
                    "required": ["ok"],
                    "properties": {"ok": {"type": "boolean"}},
                    "additionalProperties": False,
                },
                "output_schema": {
                    "type": "object",
                    "required": ["ok"],
                    "properties": {"ok": {"type": "boolean"}},
                    "additionalProperties": False,
                },
                "validation_cases": [{"args": {"ok": True}, "expect_output": {"ok": True}}],
            },
        )
    )
    capability_id = json.loads(created.output)["capability_id"]
    context = type(
        "Context",
        (),
        {"workspace": WorkspaceSpec(id="repo", root=str(tmp_path))},
    )()
    original_executor = fabric.executor_for(capability_id, task_id="task-promotion-failure")
    for index, value in enumerate((True, False, True)):
        result = await original_executor.invoke(
            CapabilityRequest(
                capability_id=capability_id,
                task_id="task-promotion-failure",
                call_id=f"promotion-failure-use-{index}",
                session_id=f"promotion-failure-session-{index % 2}",
                arguments={"ok": value},
            ),
            context=context,
        )
        assert result.status is CapabilityResultStatus.OK
    await _record_passing_verifications(
        engine,
        capability_id,
        "task-promotion-failure",
        [f"promotion-failure-use-{index}" for index in range(3)],
    )

    async def fail_save(*args, **kwargs):
        del args, kwargs
        raise OSError("durable store unavailable")

    monkeypatch.setattr(store, "save", fail_save)
    promoted = await capability.invoke(
        CapabilityRequest(
            capability_id="synthesis",
            task_id="task-promotion-failure",
            call_id="promote-promotion-failure",
            arguments={
                "operation": "promote",
                "capability_id": capability_id,
                "scope": "project",
            },
        ),
        context=context,
    )

    assert promoted.status is CapabilityResultStatus.FAILED
    assert "promotion persistence failed" in (promoted.error or "")
    assert fabric.has(capability_id, task_id="task-promotion-failure")
    assert not fabric.has(capability_id, project_id="repo")
    assert fabric.executor_for(capability_id, task_id="task-promotion-failure") is original_executor
    current = engine.synthetic_for(capability_id)
    assert current is not None
    assert current.task_id == "task-promotion-failure"
    assert current.lifecycle_state != "PROMOTED"
    await db.close()


@pytest.mark.asyncio
async def test_candidate_can_be_rehydrated_and_promoted_after_restart(tmp_path):
    if os.environ.get("ATHENA_SKIP_BWRAP_HEAVY") == "1":
        pytest.skip("bwrap-heavy validation skipped for constrained host")
    db = Database(str(tmp_path / "candidate.db"))
    store = GeneratedCapabilityStore(db)

    registry = CapabilityRegistry()
    fabric = CapabilityFabric(registry, store=store)
    engine = SynthesisEngine()
    engine.bind_proof_sink(fabric.update_generated_proof)
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
                    {"args": {"value": 1}, "expect_output": {"value": 1}},
                    {"args": {"value": 2}, "expect_output": {"value": 2}},
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
    await _record_passing_verifications(
        engine,
        capability_id,
        "task-candidate",
        [f"candidate-use-{value}" for value in (10, 11, 12)],
    )
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


@pytest.mark.asyncio
async def test_generated_tool_unfamiliar_failure_repair_and_restart_reuse_qualification(tmp_path):
    """Exercise generated-tool repair and durable reuse through the real seams."""
    db_path = tmp_path / "generated-tool-qualification.db"
    db = Database(str(db_path))
    registry = CapabilityRegistry()
    store = GeneratedCapabilityStore(db)
    fabric = CapabilityFabric(registry, store=store)
    engine = SynthesisEngine()
    engine.bind_proof_sink(fabric.update_generated_proof)
    capability = SynthesisCapability(engine, fabric)
    registry.register(capability)
    dispatcher = CapabilityDispatcher(
        registry,
        PolicyEngine("autonomous"),
        fabric=fabric,
    )
    engine.bind_dispatcher(dispatcher)
    workspace = WorkspaceSpec(id="generated-repo", root=str(tmp_path / "workspace"))
    (tmp_path / "workspace").mkdir()
    context = SimpleNamespace(workspace=workspace, principal_id="qualifier")

    created = await dispatcher.dispatch(
        CapabilityRequest(
            capability_id="synthesis",
            task_id="task-generated-qualification",
            call_id="create-generated-qualification",
            origin=CapabilityRequestOrigin.MODEL,
            arguments={
                "operation": "create",
                "name": "known_or_broken",
                "description": "Recognizes a narrow input and fails safely otherwise.",
                "code": (
                    "def run(args):\n"
                    "    if args.get('kind') == 'known':\n"
                    "        return {'value': 1}\n"
                    "    return {'value': 'unfamiliar'}\n"
                ),
                "input_schema": {
                    "type": "object",
                    "required": ["kind"],
                    "properties": {"kind": {"type": "string"}},
                    "additionalProperties": False,
                },
                "output_schema": {
                    "type": "object",
                    "required": ["value"],
                    "properties": {"value": {"type": "integer"}},
                    "additionalProperties": False,
                },
                "validation_cases": [{"args": {"kind": "known"}, "expect_output": {"value": 1}}],
            },
        ),
        workspace=workspace,
    )
    assert created.status is CapabilityResultStatus.OK, created.error
    first_id = json.loads(created.output)["capability_id"]
    first_executor = fabric.executor_for(first_id, task_id="task-generated-qualification")
    first_call_ids = []
    for index, (session_id, kind) in enumerate(
        (("session-one", "known"), ("session-two", "known"), ("session-two", "known")), start=1
    ):
        call_id = f"initial-live-{index}"
        first_call_ids.append(call_id)
        initial = await first_executor.invoke(
            CapabilityRequest(
                capability_id=first_id,
                task_id="task-generated-qualification",
                session_id=session_id,
                call_id=call_id,
                arguments={"kind": kind},
            ),
            context=context,
        )
        assert initial.status is CapabilityResultStatus.OK, initial.error
    for call_id in first_call_ids:
        await engine.observe_event(
            make_event(
                "CapabilityCompleted",
                {"call_id": call_id, "capability_id": first_id},
                task_id="task-generated-qualification",
            )
        )
        await engine.observe_event(
            make_event(
                "VerificationCompleted",
                {"passed": True},
                task_id="task-generated-qualification",
            )
        )
    first_promoted = await capability.invoke(
        CapabilityRequest(
            capability_id="synthesis",
            task_id="task-generated-qualification",
            call_id="promote-original-generated",
            origin=CapabilityRequestOrigin.USER_DIRECT,
            arguments={
                "operation": "promote",
                "capability_id": first_id,
                "scope": "project",
            },
        ),
        context=context,
    )
    assert first_promoted.status is CapabilityResultStatus.OK, first_promoted.error
    await fabric.flush()
    assert fabric.has(first_id, project_id=workspace.id)

    unfamiliar = await dispatcher.dispatch(
        CapabilityRequest(
            capability_id=first_id,
            task_id="task-generated-qualification",
            call_id="unfamiliar-generated-call",
            origin=CapabilityRequestOrigin.MODEL,
            arguments={"kind": "changed"},
        ),
        workspace=workspace,
    )
    assert unfamiliar.status is CapabilityResultStatus.FAILED
    failure = unfamiliar.metadata["generated_failure"]
    assert failure["failure_class"] == "contract_mismatch"
    assert failure["repairable"] is True
    assert failure["repair_operation"] == "synthesis.repair"

    repaired = await dispatcher.dispatch(
        CapabilityRequest(
            capability_id="synthesis",
            task_id="task-generated-qualification",
            call_id="repair-generated-call",
            origin=CapabilityRequestOrigin.MODEL,
            arguments={
                "operation": "repair",
                "capability_id": first_id,
                "name": "known_or_broken_v2",
                "description": "Recognizes the changed input after repair.",
                "code": (
                    "def run(args):\n"
                    "    return {'value': 1 if args.get('kind') in {'known', 'changed'} else 0}\n"
                ),
                "validation_cases": [{"args": {"kind": "changed"}, "expect_output": {"value": 1}}],
            },
        ),
        workspace=workspace,
    )
    assert repaired.status is CapabilityResultStatus.OK, repaired.error
    repaired_id = json.loads(repaired.output)["capability_id"]
    assert repaired_id != first_id
    assert json.loads(repaired.output)["proof"]["supersedes"] == [first_id]

    executor = fabric.executor_for(repaired_id, task_id="task-generated-qualification")
    call_ids = []
    for index, kind in enumerate(("known", "changed", "third"), start=1):
        call_id = f"repaired-live-{index}"
        call_ids.append(call_id)
        live = await executor.invoke(
            CapabilityRequest(
                capability_id=repaired_id,
                task_id="task-generated-qualification",
                call_id=call_id,
                arguments={"kind": kind},
            ),
            context=context,
        )
        assert live.status is CapabilityResultStatus.OK, live.error
        assert json.loads(live.output)["value"] == (0 if kind == "third" else 1)

    for call_id in call_ids:
        await engine.observe_event(
            make_event(
                "CapabilityCompleted",
                {"call_id": call_id, "capability_id": repaired_id},
                task_id="task-generated-qualification",
            )
        )
        await engine.observe_event(
            make_event(
                "VerificationCompleted",
                {"passed": True},
                task_id="task-generated-qualification",
            )
        )
    second_task_id = "task-generated-qualification"
    second_session_id = "session-generated-qualification-second"
    second_executor = fabric.executor_for(repaired_id, task_id=second_task_id)
    second_call = await second_executor.invoke(
        CapabilityRequest(
            capability_id=repaired_id,
            task_id=second_task_id,
            session_id=second_session_id,
            call_id="repaired-second-context",
            arguments={"kind": "second-context"},
        ),
        context=context,
    )
    assert second_call.status is CapabilityResultStatus.OK, second_call.error
    await engine.observe_event(
        make_event(
            "CapabilityCompleted",
            {"call_id": "repaired-second-context", "capability_id": repaired_id},
            task_id=second_task_id,
        )
    )
    await engine.observe_event(
        make_event(
            "VerificationCompleted",
            {"passed": True},
            task_id=second_task_id,
        )
    )

    promoted = await capability.invoke(
        CapabilityRequest(
            capability_id="synthesis",
            task_id="task-generated-qualification",
            call_id="promote-generated-qualification",
            origin=CapabilityRequestOrigin.USER_DIRECT,
            arguments={
                "operation": "promote",
                "capability_id": repaired_id,
                "scope": "project",
            },
        ),
        context=context,
    )
    assert promoted.status is CapabilityResultStatus.OK, promoted.error
    await fabric.flush()
    assert fabric.has(repaired_id, project_id="generated-repo")

    await db.close()
    restarted_db = Database(str(db_path))
    restarted_store = GeneratedCapabilityStore(restarted_db)
    restarted_fabric = CapabilityFabric(CapabilityRegistry(), store=restarted_store)
    restarted_engine = SynthesisEngine()
    loaded = await restarted_fabric.load_persisted(
        lambda generated: restarted_engine.restore_executor(
            generated,
            proof_sink=restarted_fabric.update_generated_proof,
            workspace_root=workspace.root,
        ),
        project_id=workspace.id,
        user_id="qualifier",
    )
    assert loaded == [repaired_id]
    assert restarted_fabric.has(repaired_id, project_id=workspace.id)
    restarted_executor = restarted_fabric.executor_for(
        repaired_id, project_id=workspace.id, user_id="qualifier"
    )
    reused = await restarted_executor.invoke(
        CapabilityRequest(
            capability_id=repaired_id,
            task_id="task-after-restart",
            call_id="reused-after-restart",
            arguments={"kind": "after-restart"},
        ),
        context=context,
    )
    assert reused.status is CapabilityResultStatus.OK, reused.error
    assert json.loads(reused.output) == {"value": 0}
    await restarted_db.close()
