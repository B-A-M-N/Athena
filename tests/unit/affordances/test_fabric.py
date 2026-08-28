from __future__ import annotations

from dataclasses import replace

import pytest

from athena.affordances import (
    AffordanceScope,
    DependencyRequirement,
    CapabilityFabric,
    GeneratedCapability,
    GeneratedCapabilityStore,
)
from athena.capabilities.registry import CapabilityRegistry
from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityOrigin,
    CapabilityResult,
    CapabilityResultStatus,
)
from athena.protocol.tasks import WorkspaceSpec
from athena.state.database import Database
from athena.synthesis.engine import SynthesisEngine


class _Executor:
    def __init__(self, capability_id: str) -> None:
        self.descriptor = CapabilityDescriptor(
            id=capability_id,
            description=capability_id,
            input_schema={"type": "object"},
            origin=CapabilityOrigin.PROJECT,
        )

    async def invoke(self, request, **kwargs):
        return CapabilityResult(
            request.call_id,
            request.capability_id,
            CapabilityResultStatus.OK,
        )


class _CandidateProofStore:
    def __init__(self, candidate):
        self.candidate = candidate
        self.updated = None

    async def get(self, capability_id, *, task_id=None, **kwargs):
        del kwargs
        if capability_id == self.candidate.id and task_id == self.candidate.task_scope:
            return self.candidate
        return None

    async def update_proof(self, capability_id, proof_record):
        assert capability_id == self.candidate.id
        self.updated = proof_record
        return self.candidate


def _generated(
    scope: AffordanceScope,
    *,
    project: str | None = None,
    user: str | None = None,
    capability_id: str = "generated.echo",
):
    return GeneratedCapability(
        id=capability_id,
        name="echo",
        description="echo",
        implementation="def run(args):\n    return args\n",
        input_schema={"type": "object"},
        scope=scope,
        project_scope=project,
        task_scope="task-1" if scope is AffordanceScope.TASK else None,
        effective_authority=frozenset({"READ_LOCAL"}),
        user_scope=user,
        validation_state="PROMOTED",
        proof_record={"all_passed": True},
    )


def test_overlay_precedence_and_task_cleanup():
    registry = CapabilityRegistry()
    registry.register(_Executor("shared"))
    fabric = CapabilityFabric(registry)
    user = _Executor("shared")
    project = _Executor("shared")
    task = _Executor("shared")

    fabric.register_user("athena", user)
    fabric.register_project("repo", project)
    fabric.register_task("task-1", task)

    assert fabric.executor_for("shared", user_id="athena").__class__ is user.__class__
    assert (
        fabric.executor_for("shared", project_id="repo", user_id="athena").__class__
        is project.__class__
    )
    assert (
        fabric.executor_for(
            "shared", task_id="task-1", project_id="repo", user_id="athena"
        ).__class__
        is task.__class__
    )

    fabric.unregister_task("task-1")
    assert (
        fabric.executor_for(
            "shared", task_id="task-1", project_id="repo", user_id="athena"
        ).__class__
        is project.__class__
    )


def test_search_uses_current_generated_prerequisite_evidence(tmp_path):
    registry = CapabilityRegistry()
    registry.register(_Executor("native.parser"))
    fabric = CapabilityFabric(registry)
    generated = GeneratedCapability(
        id="generated.parser",
        name="parser",
        description="Parse project output",
        implementation="def run(args):\n    return args\n",
        input_schema={"type": "object"},
        scope=AffordanceScope.PROJECT,
        project_scope="repo",
        validation_state="PROMOTED",
        lifecycle_state="PROMOTED",
        required_capabilities=("native.parser", "missing.parser"),
    )
    fabric.register_project("repo", _Executor(generated.id), generated=generated)

    result = fabric.search(
        "parser",
        project_id="repo",
        user_id="athena",
        workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
    )

    item = next(item for item in result if item["id"] == generated.id)
    assert item["optimizer"]["dependency_available"] is False
    assert item["optimizer"]["environment_compatible"] is False


def test_search_rejects_missing_locked_generated_dependency(tmp_path):
    generated = GeneratedCapability(
        id="generated.package",
        name="package parser",
        description="Parse with a locked package",
        implementation="def run(args):\n    return args\n",
        input_schema={"type": "object"},
        scope=AffordanceScope.PROJECT,
        project_scope="repo",
        validation_state="PROMOTED",
        lifecycle_state="PROMOTED",
        required_dependencies=(DependencyRequirement(name="demo-package"),),
        dependency_lock={"environment_fingerprint": "expected"},
    )
    fabric = CapabilityFabric(CapabilityRegistry())
    fabric.register_project("repo", _Executor(generated.id), generated=generated)

    item = fabric.search(
        "package",
        project_id="repo",
        user_id="athena",
        workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
    )[0]
    assert item["optimizer"]["dependency_available"] is False
    assert item["optimizer"]["environment_compatible"] is False


def test_search_ranks_generated_tools_by_verified_reuse_and_verification():
    fabric = CapabilityFabric(CapabilityRegistry())
    common = {
        "name": "parser",
        "description": "Parse project output",
        "input_schema": {"type": "object"},
        "scope": AffordanceScope.PROJECT,
        "project_scope": "repo",
        "validation_state": "PROMOTED",
        "lifecycle_state": "PROMOTED",
        "quality_score": 0.0,
    }
    unproven = GeneratedCapability(
        id="generated.parser.unproven",
        implementation="def run(args):\n    return {'value': 1}\n",
        **common,
    )
    proven = GeneratedCapability(
        id="generated.parser.proven",
        implementation="def run(args):\n    return {'value': 2}\n",
        proof_record={
            "metric_provenance": {
                "reuse_count": "canonical_generated_invocation",
                "downstream_verifications": "canonical_passing_verification",
            },
            "reuse_count": 4,
            "downstream_verifications": 2,
        },
        use_count=4,
        success_count=4,
        **common,
    )
    fabric.register_project("repo", _Executor(unproven.id), generated=unproven)
    fabric.register_project("repo", _Executor(proven.id), generated=proven)

    result = fabric.search(
        "parser",
        project_id="repo",
        user_id="athena",
        workspace=WorkspaceSpec(id="repo", root="/tmp"),
    )
    assert [item["id"] for item in result[:2]] == [
        proven.id,
        unproven.id,
    ]
    assert result[0]["optimizer"]["downstream_verifications"] == 2


@pytest.mark.asyncio
async def test_candidate_receives_event_derived_proof_after_retention():
    candidate = _generated(
        AffordanceScope.CANDIDATE,
        capability_id="generated.candidate-proof",
    )
    candidate = replace(candidate, task_scope="task-1")
    store = _CandidateProofStore(candidate)
    fabric = CapabilityFabric(CapabilityRegistry(), store=store)
    task_record = GeneratedCapability(
        id=candidate.id,
        name=candidate.name,
        description=candidate.description,
        implementation=candidate.implementation,
        input_schema=candidate.input_schema,
        scope=AffordanceScope.TASK,
        task_scope="task-1",
        validation_state="VALIDATED",
    )
    fabric.register_task("task-1", _Executor(task_record.id), generated=task_record)

    await fabric.update_generated_proof(
        task_record.id,
        {"downstream_verifications": 1, "usage": {"uses": 3}},
    )
    assert store.updated["downstream_verifications"] == 1


async def test_generated_store_round_trips_and_checks_hashes(tmp_path):
    path = str(tmp_path / "fabric.db")
    db = Database(path)
    store = GeneratedCapabilityStore(db)
    capability = _generated(AffordanceScope.PROJECT, project="repo")
    await store.save(capability, owner="repo")
    loaded = await store.get(capability.id, project_id="repo")
    assert loaded is not None
    assert loaded.implementation == capability.implementation
    assert loaded.code_hash == capability.code_hash
    assert (await store.list(project_id="repo"))[0].id == capability.id
    await db.close()

    db = Database(path)
    loaded = await GeneratedCapabilityStore(db).get(capability.id, project_id="repo")
    assert loaded is not None
    assert await GeneratedCapabilityStore(db).get(capability.id, project_id="other") is None
    await db.close()


async def test_generated_store_scopes_user_records(tmp_path):
    db = Database(str(tmp_path / "scopes.db"))
    store = GeneratedCapabilityStore(db)
    await store.save(
        _generated(AffordanceScope.USER, user="alice", capability_id="gen.alice"),
        owner="alice",
    )
    await store.save(
        _generated(AffordanceScope.USER, user="bob", capability_id="gen.bob"),
        owner="bob",
    )

    assert [c.id for c in await store.list(user_id="alice")] == ["gen.alice"]
    assert [c.id for c in await store.list(user_id="bob")] == ["gen.bob"]
    assert await store.list(user_id="mallory") == []
    await db.close()


async def test_generated_candidate_lifecycle_is_durable_and_gc_explicit(tmp_path):
    db = Database(str(tmp_path / "candidate.db"))
    store = GeneratedCapabilityStore(db)
    candidate = GeneratedCapability(
        id="gen.candidate",
        name="candidate",
        description="candidate lifecycle",
        implementation="def run(args):\n    return args\n",
        input_schema={"type": "object"},
        scope=AffordanceScope.CANDIDATE,
        task_scope="task-1",
        lifecycle_state="CANDIDATE",
        proof_record={"all_passed": True},
        quality_score=0.8,
        dependency_lock={"fingerprint": "env-1"},
    )
    await store.save(candidate, owner="task-1")
    await store.save(
        GeneratedCapability.from_record(
            {
                **candidate.to_record(),
                "use_count": 2,
                "success_count": 2,
                "proof_record": {"all_passed": True, "usage": {"uses": 2}},
            }
        ),
        owner="task-1",
    )
    loaded = await store.get(candidate.id, task_id="task-1")
    assert loaded is not None
    assert loaded.lifecycle_state == "CANDIDATE"
    assert loaded.dependency_lock["fingerprint"] == "env-1"
    assert await store.history(candidate.id, owner="task-1")

    await store.transition(candidate.id, "PROMOTED", owner="task-1", reason="reviewed")
    promoted = await store.get(candidate.id, task_id="task-1")
    assert promoted is not None and promoted.lifecycle_state == "PROMOTED"
    assert any(
        event.get("to") == "PROMOTED" for event in await store.history(candidate.id, owner="task-1")
    )

    assert await store.disable(candidate.id, owner="task-1") is True
    assert await store.get(candidate.id, task_id="task-1") is None
    history = await store.history(candidate.id, owner="task-1")
    assert any(event.get("to") == "DEPRECATED" for event in history)
    assert await store.garbage_collect() == 1
    assert await store.history(candidate.id, owner="task-1") == []
    await db.close()


async def test_candidate_can_be_explicitly_promoted_without_cross_scope_collision(tmp_path):
    db = Database(str(tmp_path / "candidate-promotion.db"))
    store = GeneratedCapabilityStore(db)
    candidate = GeneratedCapability(
        id="gen.promote",
        name="promote",
        description="candidate promotion",
        implementation="def run(args):\n    return args\n",
        input_schema={"type": "object"},
        scope=AffordanceScope.CANDIDATE,
        task_scope="task-1",
        provenance={"task_id": "task-1"},
        lifecycle_state="CANDIDATE",
        validation_state="VALIDATED",
        proof_record={"all_passed": True},
    )
    await store.save(candidate, owner="task-1")

    promoted = GeneratedCapability.from_record(
        {
            **candidate.to_record(),
            "scope": AffordanceScope.PROJECT.value,
            "task_scope": None,
            "project_scope": "repo",
            "lifecycle_state": "PROMOTED",
            "validation_state": "PROMOTED",
            "provenance": {"task_id": "task-1", "promoted_from": "task"},
        }
    )
    await store.save(promoted, owner="repo")

    loaded = await store.get(promoted.id, project_id="repo")
    assert loaded is not None
    assert loaded.scope is AffordanceScope.PROJECT
    assert loaded.lifecycle_state == "PROMOTED"
    assert any(
        event.get("to") == "PROMOTED" for event in await store.history(promoted.id, owner="repo")
    )
    await db.close()


async def test_fabric_rehydrates_only_visible_promoted_records(tmp_path):
    db = Database(str(tmp_path / "rehydrate.db"))
    store = GeneratedCapabilityStore(db)
    await store.save(
        _generated(AffordanceScope.PROJECT, project="repo", capability_id="gen.repo"),
        owner="repo",
    )
    await store.save(
        _generated(AffordanceScope.PROJECT, project="other", capability_id="gen.other"),
        owner="other",
    )
    fabric = CapabilityFabric(CapabilityRegistry(), store=store)
    loaded = await fabric.load_persisted(
        lambda generated: _Executor(generated.id),
        project_id="repo",
        user_id="athena",
    )

    assert loaded == ["gen.repo"]
    assert fabric.has("gen.repo", project_id="repo")
    assert not fabric.has("gen.other", project_id="repo")
    await db.close()


@pytest.mark.athena_scenario("AUTH-002")
def test_restore_executor_rechecks_source_and_output_contract():
    generated = GeneratedCapability(
        id="gen.restore",
        name="restore",
        description="rehydration contract",
        implementation='def run(args):\n    return {"ok": True}\n',
        input_schema={"type": "object"},
        output_schema={
            "type": "object",
            "required": ["ok"],
            "properties": {"ok": {"type": "boolean"}},
            "additionalProperties": False,
        },
        scope=AffordanceScope.PROJECT,
        project_scope="repo",
        validation_state="PROMOTED",
        proof_record={"all_passed": True},
    )

    executor = SynthesisEngine().restore_executor(generated)
    assert executor.descriptor.output_schema == generated.output_schema


@pytest.mark.athena_scenario("AUTH-002")
def test_restore_executor_rejects_persisted_source_escape():
    generated = GeneratedCapability(
        id="gen.invalid",
        name="invalid",
        description="invalid rehydration",
        implementation=("import subprocess\ndef run(args):\n    return subprocess.run(args)\n"),
        input_schema={"type": "object"},
        scope=AffordanceScope.PROJECT,
        project_scope="repo",
        validation_state="PROMOTED",
        proof_record={"all_passed": True},
    )

    with pytest.raises(ValueError, match="source checks"):
        SynthesisEngine().restore_executor(generated)


@pytest.mark.athena_scenario("AUTH-002")
def test_restore_executor_rejects_persisted_authority_outside_profile():
    generated = GeneratedCapability(
        id="gen.authority",
        name="authority",
        description="invalid authority metadata",
        implementation='def run(args):\n    return {"ok": True}\n',
        input_schema={"type": "object"},
        scope=AffordanceScope.PROJECT,
        project_scope="repo",
        effective_authority=frozenset({"PRIVILEGED"}),
        validation_state="PROMOTED",
        proof_record={"all_passed": True},
    )

    with pytest.raises(ValueError, match="sandbox profile"):
        SynthesisEngine().restore_executor(generated)
