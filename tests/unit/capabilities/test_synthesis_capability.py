from __future__ import annotations

import json

import pytest

from athena.affordances import CapabilityFabric, GeneratedCapabilityStore
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
    invocation = await executor.invoke(
        CapabilityRequest(
            capability_id=generated_id,
            arguments={"msg": "world"},
            task_id="task-1",
            call_id="call-1",
        ),
        context=type("Context", (), {
            "workspace": WorkspaceSpec(id="repo", root=str(tmp_path)),
        })(),
    )
    assert invocation.status is CapabilityResultStatus.OK
    assert json.loads(invocation.output) == {"echo": "world"}

    promoted = await capability.invoke(
        CapabilityRequest(
            capability_id="synthesis", task_id="task-1", call_id="promote-1",
            arguments={
                "operation": "promote", "capability_id": generated_id,
                "scope": "project",
            },
        ),
        context=type("Context", (), {
            "workspace": WorkspaceSpec(id="repo", root=str(tmp_path)),
        })(),
    )
    assert promoted.status is CapabilityResultStatus.OK
    assert fabric.has(generated_id, project_id="repo")
    assert not fabric.has(generated_id, task_id="task-1")


@pytest.mark.asyncio
async def test_synthesis_promotion_is_policy_checked(tmp_path):
    registry = CapabilityRegistry()
    fabric = CapabilityFabric(registry)
    engine = SynthesisEngine()
    capability = SynthesisCapability(engine, fabric)
    registry.register(capability)
    dispatcher = CapabilityDispatcher(
        registry, PolicyEngine(AutonomyLevel.CODING), fabric=fabric,
    )
    create = await capability.invoke(CapabilityRequest(
        capability_id="synthesis", task_id="task-4", call_id="create-4",
        arguments={
            "operation": "create", "name": "promotable_helper",
            "description": "A helper for promotion testing",
            "code": "def run(args):\n    return {'ok': True}\n",
            "input_schema": {"type": "object"},
            "effects": ["READ_LOCAL"],
            "validation_cases": [{"args": {}}],
        },
    ))
    generated_id = json.loads(create.output)["capability_id"]
    result = await dispatcher.dispatch(
        CapabilityRequest(
            capability_id="synthesis", task_id="task-4", call_id="promote-4",
            origin=CapabilityRequestOrigin.MODEL,
            arguments={
                "operation": "promote", "capability_id": generated_id,
                "scope": "project",
            },
        ),
        workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
    )
    assert result.status is CapabilityResultStatus.OK
    assert fabric.has(generated_id, project_id="repo")


@pytest.mark.asyncio
async def test_synthesis_requires_task_scope(tmp_path):
    capability = SynthesisCapability(SynthesisEngine(), CapabilityFabric(CapabilityRegistry()))
    result = await capability.invoke(CapabilityRequest(
        capability_id="synthesis", task_id=None, call_id="create-2",
        arguments={"operation": "create"},
    ))
    assert result.status is CapabilityResultStatus.FAILED
    assert "task scope" in (result.error or "")


@pytest.mark.asyncio
async def test_synthesis_create_uses_canonical_dispatcher(tmp_path):
    registry = CapabilityRegistry()
    fabric = CapabilityFabric(registry)
    registry.register(SynthesisCapability(SynthesisEngine(), fabric))
    dispatcher = CapabilityDispatcher(
        registry, PolicyEngine(AutonomyLevel.CODING), fabric=fabric,
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
async def test_synthesis_generates_strict_input_schema_from_fixtures():
    fabric = CapabilityFabric(CapabilityRegistry())
    capability = SynthesisCapability(SynthesisEngine(), fabric)
    result = await capability.invoke(CapabilityRequest(
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
    ))

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

    created = await capability.invoke(CapabilityRequest(
        capability_id="synthesis", task_id="task-proof", call_id="create-proof",
        arguments={
            "operation": "create", "name": "proof_helper",
            "description": "A helper whose live proof is durable",
            "code": "def run(args):\n    return {'ok': args['ok']}\n",
            "input_schema": {
                "type": "object", "required": ["ok"],
                "properties": {"ok": {"type": "boolean"}},
                "additionalProperties": False,
            },
            "validation_cases": [{"args": {"ok": True}}],
        },
    ))
    generated_id = json.loads(created.output)["capability_id"]
    context = type("Context", (), {
        "workspace": WorkspaceSpec(id="repo", root=str(tmp_path)),
    })()
    promoted = await capability.invoke(CapabilityRequest(
        capability_id="synthesis", task_id="task-proof", call_id="promote-proof",
        arguments={
            "operation": "promote", "capability_id": generated_id,
            "scope": "project",
        },
    ), context=context)
    assert promoted.status is CapabilityResultStatus.OK

    executor = fabric.executor_for(generated_id, project_id="repo")
    result = await executor.invoke(CapabilityRequest(
        capability_id=generated_id, task_id="task-proof", call_id="use-proof",
        arguments={"ok": False},
    ), context=context)
    assert result.status is CapabilityResultStatus.OK

    loaded = await GeneratedCapabilityStore(db).get(
        generated_id, project_id="repo",
    )
    assert loaded is not None
    assert loaded.proof_record["usage"] == {
        "uses": 1, "successes": 1, "failures": 0,
    }
    await db.close()
