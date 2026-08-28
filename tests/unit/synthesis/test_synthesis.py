"""Unit tests for athena.synthesis.engine (SynthesisEngine)."""

from __future__ import annotations

import json
import base64
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from athena.capabilities.registry import CapabilityRegistry
from athena.capabilities.dispatcher import CapabilityDispatcher
from athena.capabilities.fs import FilesystemCapability
from athena.capabilities.synthesis import SynthesisCapability
from athena.affordances import CapabilityFabric
from athena.affordances.models import (
    AffordanceScope,
    DependencyRequirement,
    EvidenceDependency,
    GeneratedCapability,
)
from athena.protocol.capabilities import (
    CapabilityRequest,
    CapabilityRequestOrigin,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)
from athena.protocol.events import make_event
from athena.protocol.errors import CapabilityUnavailable
from athena.protocol.tasks import MutationMode, PathRule, WorkspaceSpec
from athena.policy.engine import PolicyEngine
from athena.execution.dependencies import environment_fingerprint
from athena.reality import RealityGate
from athena.shadow.engine import ShadowEngine
from athena.synthesis.engine import SynthesisEngine
from athena.synthesis.runtime import GeneratedHostError, GeneratedToolHost
from athena.research.models import EvidenceObject, SourceRecord
from athena.research.store import ResearchStore
from athena.state.database import Database

GOOD_CODE = "def run(args):\n    return {'echo': args.get('msg', '')}\n"
BAD_CODE = "def run(args):\n    raise RuntimeError('boom')\n"


class _HostDispatcher:
    def __init__(self):
        self.calls = []
        self.progress = []

    async def dispatch(self, request, **kwargs):
        self.calls.append((request, kwargs))
        return CapabilityResult(
            request.call_id,
            request.capability_id,
            CapabilityResultStatus.OK,
            output=json.dumps({"content": "from governed capability"}),
        )

    async def emit_progress(self, **kwargs):
        self.progress.append(kwargs)


def _write_locked_dependency(root):
    target = root / ".athena" / "dependencies"
    package = target / "demo_dep"
    dist_info = target / "demo_dep-1.2.3.dist-info"
    package.mkdir(parents=True)
    dist_info.mkdir()
    content = b"VALUE = 'from locked dependency'\n"
    (package / "__init__.py").write_bytes(content)
    (dist_info / "METADATA").write_text("Metadata-Version: 2.1\nName: demo-dep\nVersion: 1.2.3\n")
    digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest())
    digest = digest.rstrip(b"=").decode("ascii")
    record_line = f"demo_dep/__init__.py,sha256={digest},{len(content)}\n"
    (dist_info / "RECORD").write_text(record_line)
    package_record = {
        "name": "demo-dep",
        "resolved_version": "1.2.3",
        "record_hashes": [f"demo_dep/__init__.py:sha256={digest}"],
    }
    environment = environment_fingerprint((package_record,))
    (root / ".athena" / "dependencies.lock.json").write_text(
        json.dumps(
            {
                "format": 1,
                "packages": {
                    "demo-dep": {
                        "resolved_version": "1.2.3",
                        "record_hashes": package_record["record_hashes"],
                        "environment_fingerprint": environment,
                    }
                },
            }
        )
    )
    return environment


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
    result = await engine.validate(cap, [{"args": {"msg": "hi"}, "expect_output_contains": "hi"}])
    assert result.validation["all_passed"] is True, json.dumps(
        result.validation, indent=2, default=str
    )
    assert result.validation["cases_passed"] == 1


@pytest.mark.asyncio
async def test_generated_validation_emits_fixture_progress(tmp_path):
    dispatcher = _HostDispatcher()
    engine = SynthesisEngine(dispatcher=dispatcher)
    cap = _make_cap(engine, name="progress")

    result = await engine.validate(
        cap,
        [
            {"args": {"msg": "one"}, "expect_output_contains": "one"},
            {"args": {"msg": "two"}, "expect_output_contains": "two"},
        ],
        workspace_root=str(tmp_path),
        task_id="task-progress",
    )

    assert result.validation["all_passed"] is True
    assert [item["value"] for item in dispatcher.progress] == [1, 2]
    assert all(item["total"] == 2 and item["unit"] == "fixtures" for item in dispatcher.progress)


@pytest.mark.asyncio
async def test_generated_proof_counts_only_passing_canonical_verification():
    engine = SynthesisEngine()
    cap = _make_cap(engine, name="verified_metric")
    engine._synthetic[cap.id] = cap
    persisted = []
    engine.bind_proof_sink(lambda capability_id, proof: persisted.append((capability_id, proof)))

    await engine.observe_event(
        make_event(
            "CapabilityCompleted",
            {"call_id": "generated-call", "capability_id": cap.id},
            task_id="task-proof",
        )
    )
    await engine.observe_event(
        make_event(
            "VerificationCompleted",
            {"passed": False},
            task_id="task-proof",
        )
    )
    assert cap.downstream_verifications == 0
    assert persisted == []

    await engine.observe_event(
        make_event(
            "VerificationCompleted",
            {"passed": True},
            task_id="task-proof",
        )
    )
    assert cap.downstream_verifications == 1
    assert persisted[-1][0] == cap.id
    assert persisted[-1][1]["downstream_verifications"] == 1

    # The same verification event cannot be replayed as a second proof for
    # the already-accounted invocation.
    await engine.observe_event(
        make_event(
            "VerificationCompleted",
            {"passed": True},
            task_id="task-proof",
        )
    )
    assert cap.downstream_verifications == 1


@pytest.mark.asyncio
async def test_generated_proof_replays_durable_event_history():
    engine = SynthesisEngine()
    cap = _make_cap(engine, name="replayed_metric")
    engine._synthetic[cap.id] = cap
    events = [
        make_event(
            "CapabilityCompleted",
            {"call_id": "replayed-call", "capability_id": cap.id},
            task_id="task-replay",
        ),
        make_event(
            "VerificationCompleted",
            {"passed": True},
            task_id="task-replay",
        ),
    ]
    for rowid, event in enumerate(events, start=1):
        object.__setattr__(event, "_rowid", rowid)

    class _Events:
        async def list_recent(self, *, after_rowid, limit):
            return [event for event in events if getattr(event, "_rowid", 0) > after_rowid][:limit]

    await engine.replay_event_metrics(_Events())
    assert cap.downstream_verifications == 1


@pytest.mark.asyncio
async def test_generated_capability_evidence_dependencies_become_stale(tmp_path):
    db = Database(str(tmp_path / "evidence.db"))
    await db._ensure_ready()
    research = ResearchStore(db)
    source = SourceRecord.for_uri(
        "https://example.test/api",
        content_hash="source-v1",
    )
    evidence = EvidenceObject.for_content(
        source_id=source.id,
        extracted_claim="the adapter accepts JSON",
        exact_supporting_excerpt="accepts JSON",
    )
    await research.save_source(source)
    await research.save_evidence(evidence)
    cap = SynthesisEngine().synthesize(
        name="evidence_adapter",
        description="adapter tied to a captured contract",
        code=GOOD_CODE,
        input_schema={"type": "object", "properties": {}},
        task_id="task-evidence",
        evidence_dependencies=(
            EvidenceDependency(
                requirement="API JSON contract",
                evidence_id=evidence.id,
                content_hash="source-v1",
            ),
        ),
    )
    engine = SynthesisEngine()
    current = await engine.evidence_status(cap, research)
    assert current["status"] == "CURRENT"

    await research.save_source(
        SourceRecord.for_uri(
            source.canonical_uri,
            content_hash="source-v2",
        )
    )
    stale = await engine.evidence_status(cap, research)
    assert stale["status"] == "STALE"
    assert stale["dependencies"][0]["status"] == "source_revision_changed"
    await db.close()


@pytest.mark.asyncio
async def test_generated_host_calls_reenter_dispatcher_from_child(tmp_path):
    dispatcher = _HostDispatcher()
    engine = SynthesisEngine(dispatcher=dispatcher)
    cap = engine.synthesize(
        name="composed_reader",
        description="composes a native read capability",
        code=(
            "def run(args):\n"
            "    value = athena.call('fs', {'operation': 'read', 'path': args['path']})\n"
            "    return {'value': value}\n"
        ),
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        task_id="task-host",
    )
    workspace = WorkspaceSpec(id="repo", root=str(tmp_path))

    result = await engine.validate(
        cap,
        [{"args": {"path": "input.txt"}, "expect_output_contains": "from governed capability"}],
        workspace_root=str(tmp_path),
        workspace=workspace,
        task_id="task-host",
        session_id="session-host",
    )

    assert result.validation["all_passed"] is True, json.dumps(
        result.validation, indent=2, default=str
    )
    assert len(dispatcher.calls) == 1
    request, call_context = dispatcher.calls[0]
    assert request.origin is CapabilityRequestOrigin.GENERATED
    assert request.task_id == "task-host"
    assert request.session_id == "session-host"
    assert call_context["workspace"].root != str(tmp_path)


@pytest.mark.asyncio
async def test_generated_host_enforces_validated_capability_ceiling(tmp_path):
    dispatcher = _HostDispatcher()
    host = GeneratedToolHost(
        dispatcher=dispatcher,
        workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
        task_id="task-host-ceiling",
        allowed_capabilities=frozenset({"fs"}),
    )

    with pytest.raises(GeneratedHostError, match="was not declared"):
        await host.call("execute", {"language": "shell", "code": "id"})
    assert dispatcher.calls == []


@pytest.mark.asyncio
async def test_generated_host_can_use_declared_native_write_ceiling(tmp_path):
    registry = CapabilityRegistry()
    registry.register(FilesystemCapability())
    dispatcher = CapabilityDispatcher(registry, PolicyEngine("autonomous"))
    engine = SynthesisEngine(dispatcher=dispatcher)
    cap = engine.synthesize(
        name="native_writer",
        description="writes through the governed filesystem capability",
        code="def run(args):\n    return {'ok': True}\n",
        input_schema={"type": "object"},
        effects={"READ_LOCAL", "WRITE_LOCAL"},
        required_capabilities=("fs",),
    )
    assert EffectClass.WRITE_LOCAL.value in engine._runtime_effective_effects(cap)
    assert EffectClass.DELETE.value not in engine._runtime_effective_effects(cap)

    workspace = WorkspaceSpec(
        id="repo",
        root=str(tmp_path),
        writable=(PathRule(str(tmp_path)),),
    )
    host = GeneratedToolHost(
        dispatcher=dispatcher,
        workspace=workspace,
        task_id="task-writer",
        allowed_capabilities=frozenset({"fs"}),
        inherited_effects=frozenset(
            {
                EffectClass.READ_LOCAL,
                EffectClass.EXECUTE,
                EffectClass.WRITE_LOCAL,
            }
        ),
        inherited_capability_id=cap.id,
    )
    value = await host.call(
        "fs",
        {"operation": "write", "path": "output.txt", "content": "ok\n"},
    )
    assert value == "wrote 3 bytes"
    assert (tmp_path / "output.txt").read_text(encoding="utf-8") == "ok\n"


@pytest.mark.asyncio
async def test_generated_validation_rejects_undeclared_native_write(tmp_path):
    registry = CapabilityRegistry()
    registry.register(FilesystemCapability())
    dispatcher = CapabilityDispatcher(registry, PolicyEngine("autonomous"))
    engine = SynthesisEngine(dispatcher=dispatcher)
    cap = engine.synthesize(
        name="undeclared_writer",
        description="attempts a native write without declaring its effect",
        code=(
            "def run(args):\n"
            "    return athena.call('fs', {'operation': 'write', 'path': 'out.txt', 'content': 'x'})\n"
        ),
        input_schema={"type": "object"},
        required_capabilities=("fs",),
    )

    validated = await engine.validate(
        cap,
        [{"args": {}}],
        workspace_root=str(tmp_path),
        workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
        task_id="task-undeclared-writer",
        profile="autonomous",
    )

    assert validated.validation["all_passed"] is False, validated.validation
    assert any(
        "outside its parent authority ceiling" in str(detail.get("stderr"))
        for detail in validated.validation["details"]
    )


@pytest.mark.asyncio
async def test_generated_failure_exposes_bounded_repair_signal(tmp_path, monkeypatch):
    engine = SynthesisEngine()
    cap = engine.synthesize(
        name="contract_breaker",
        description="returns a value outside its validated contract",
        code="def run(args):\n    return {'value': 'wrong'}\n",
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        task_id="task-repair-signal",
    )

    async def fake_child(*args, **kwargs):
        del args, kwargs
        return '__RESULT__{"value": "wrong"}\n', "", 0

    monkeypatch.setattr(engine, "_run_child_async", fake_child)
    executor = engine._build_executor(cap)
    live = await executor.invoke(
        CapabilityRequest(
            capability_id=cap.id,
            task_id="task-repair-signal",
            call_id="repair-signal-live",
            arguments={},
        ),
        context=SimpleNamespace(
            workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
            autonomy=None,
            capability_policy=None,
            resource_budget=None,
            generated_call_depth=0,
            generated_call_chain=(),
        ),
    )
    assert live.status is CapabilityResultStatus.FAILED
    signal = live.metadata["generated_failure"]
    assert signal["failure_class"] == "contract_mismatch"
    assert signal["repairable"] is True
    assert signal["repair_operation"] == "synthesis.repair"


@pytest.mark.asyncio
async def test_generated_reuse_proof_requires_successful_contract(tmp_path, monkeypatch):
    engine = SynthesisEngine()
    cap = engine.synthesize(
        name="reuse_proof",
        description="counts only successful repeated executions",
        code=GOOD_CODE,
        input_schema={"type": "object"},
        task_id="task-reuse-proof",
    )
    calls = 0

    async def fake_child(*args, **kwargs):
        nonlocal calls
        del args, kwargs
        calls += 1
        if calls == 3:
            return "", "failed", 1
        return '__RESULT__{"echo":"ok"}\n', "", 0

    monkeypatch.setattr(engine, "_run_child_async", fake_child)
    context = SimpleNamespace(
        workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
        autonomy=None,
        capability_policy=None,
        resource_budget=None,
        generated_call_depth=0,
        generated_call_chain=(),
    )

    for call_id in ("reuse-1", "reuse-2", "reuse-3"):
        result = await engine._build_executor(cap).invoke(
            CapabilityRequest(
                capability_id=cap.id,
                task_id=cap.task_id,
                call_id=call_id,
                arguments={"msg": "same"},
            ),
            context=context,
        )
        if call_id == "reuse-3":
            assert result.status is CapabilityResultStatus.FAILED

    assert cap.uses == 3
    assert cap.successes == 2
    assert cap.reuse_count == 1


@pytest.mark.asyncio
async def test_generated_host_composes_real_filesystem_capability(tmp_path):
    (tmp_path / "input.txt").write_text("native result\n", encoding="utf-8")
    registry = CapabilityRegistry()
    registry.register(FilesystemCapability())
    dispatcher = CapabilityDispatcher(registry, PolicyEngine("offline"))
    engine = SynthesisEngine(dispatcher=dispatcher)
    cap = engine.synthesize(
        name="native_reader",
        description="reads through Athena's filesystem capability",
        code=(
            "def run(args):\n"
            "    return {'text': athena.call('fs', {'operation': 'read', 'path': args['path']})}\n"
        ),
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        task_id="task-native-host",
    )
    workspace = WorkspaceSpec(id="repo", root=str(tmp_path))

    validated = await engine.validate(
        cap,
        [{"args": {"path": "input.txt"}, "expect_output_contains": "native result"}],
        workspace_root=str(tmp_path),
        workspace=workspace,
        task_id="task-native-host",
    )
    assert validated.validation["all_passed"] is True, json.dumps(
        validated.validation, indent=2, default=str
    )
    assert validated.required_capabilities == ("fs",)

    assert engine.register_ephemeral(registry, validated) is True
    result = await registry.executor_for(validated.id).invoke(
        CapabilityRequest(
            capability_id=validated.id,
            arguments={"path": "input.txt"},
            task_id="task-native-host",
            call_id="live-host-call",
        ),
        context=SimpleNamespace(workspace=workspace),
    )
    assert result.status is CapabilityResultStatus.OK
    assert json.loads(result.output) == {"text": "native result\n"}


@pytest.mark.asyncio
async def test_persistent_generated_runtime_retains_state_and_reenters_host(
    tmp_path,
    monkeypatch,
):
    """Persistent code retains only task-local state; authority stays native."""
    dispatcher = _HostDispatcher()
    engine = SynthesisEngine(dispatcher=dispatcher)
    monkeypatch.setattr(
        "athena.synthesis.engine.sandbox_argv",
        lambda argv, **kwargs: argv,
    )
    cap = engine.synthesize(
        name="persistent_reader",
        description="retains a counter while using a governed reader",
        code=(
            "count = 0\n"
            "def run(args):\n"
            "    global count\n"
            "    count += 1\n"
            "    value = athena.call('fs', {'operation': 'read', 'path': args['path']})\n"
            "    return {'count': count, 'value': value}\n"
        ),
        runtime="python_persistent",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        task_id="task-persistent",
        required_capabilities=("fs",),
    )
    context = SimpleNamespace(
        workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
        autonomy=None,
        capability_policy=None,
        resource_budget=None,
        generated_call_depth=0,
        generated_call_chain=(),
    )
    executor = engine._build_executor(cap)
    try:
        for index, call_id in enumerate(("persistent-1", "persistent-2"), start=1):
            result = await executor.invoke(
                CapabilityRequest(
                    capability_id=cap.id,
                    task_id="task-persistent",
                    call_id=call_id,
                    arguments={"path": "input.txt"},
                ),
                context=context,
            )
            assert result.status is CapabilityResultStatus.OK
            assert json.loads(result.output) == {
                "count": index,
                "value": {"content": "from governed capability"},
            }
        assert len(dispatcher.calls) == 2
        assert all(
            request.origin is CapabilityRequestOrigin.GENERATED
            for request, _context in dispatcher.calls
        )
        assert len(engine._persistent_sessions) == 1
        await engine.close_persistent_sessions_for_task("task-persistent")
        assert engine._persistent_sessions == {}
    finally:
        await engine.close_persistent_sessions()


@pytest.mark.asyncio
async def test_persistent_generated_runtime_restarts_after_process_exit(
    tmp_path,
    monkeypatch,
):
    """A crashed generated process cannot poison later calls."""
    engine = SynthesisEngine()
    monkeypatch.setattr(
        "athena.synthesis.engine.sandbox_argv",
        lambda argv, **kwargs: argv,
    )
    cap = engine.synthesize(
        name="restartable_persistent",
        description="recovers after a generated process exits",
        code=(
            "count = 0\n"
            "def run(args):\n"
            "    global count\n"
            "    count += 1\n"
            "    if args.get('crash'):\n"
            "        raise SystemExit('intentional exit')\n"
            "    return {'count': count}\n"
        ),
        runtime="python_persistent",
        input_schema={"type": "object"},
        task_id="task-restartable",
    )
    context = SimpleNamespace(
        workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
        autonomy=None,
        capability_policy=None,
        resource_budget=None,
        generated_call_depth=0,
        generated_call_chain=(),
    )
    executor = engine._build_executor(cap)
    try:
        failed = await executor.invoke(
            CapabilityRequest(
                capability_id=cap.id,
                task_id="task-restartable",
                call_id="persistent-crash",
                arguments={"crash": True},
            ),
            context=context,
        )
        assert failed.status is CapabilityResultStatus.FAILED
        assert engine._persistent_sessions == {}

        recovered = await executor.invoke(
            CapabilityRequest(
                capability_id=cap.id,
                task_id="task-restartable",
                call_id="persistent-recovery",
                arguments={},
            ),
            context=context,
        )
        assert recovered.status is CapabilityResultStatus.OK
        assert json.loads(recovered.output) == {"count": 1}
    finally:
        await engine.close_persistent_sessions()


@pytest.mark.asyncio
async def test_validation_cases_support_workspace_fixtures_effects_and_invariants(tmp_path):
    registry = CapabilityRegistry()
    registry.register(FilesystemCapability())
    dispatcher = CapabilityDispatcher(registry, PolicyEngine("offline"))
    engine = SynthesisEngine(dispatcher=dispatcher)
    cap = engine.synthesize(
        name="fixture_reader",
        description="reads isolated validation fixtures",
        code=(
            "def run(args):\n"
            "    return {'text': athena.call('fs', {'operation': 'read', 'path': args['path']})}\n"
        ),
        input_schema={
            "type": "object",
            "required": ["path"],
            "properties": {"path": {"type": "string"}},
            "additionalProperties": False,
        },
        task_id="task-fixtures",
    )
    result = await engine.validate(
        cap,
        [
            {
                "args": {"path": "fixture.py"},
                "workspace_files": {"fixture.py": "print('fixture')\n"},
                "expect_output_contains": "fixture",
                "expect_effect": {"capability": "fs", "operation": "read"},
                "invariants": [
                    {
                        "capability": "fs",
                        "args": {"operation": "stat", "path": "fixture.py"},
                        "expect_output_contains": "is_file",
                    }
                ],
            }
        ],
        workspace_root=str(tmp_path),
        workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
        task_id="task-fixtures",
    )
    assert result.validation["all_passed"] is True, json.dumps(
        result.validation, indent=2, default=str
    )
    assert result.required_capabilities == ("fs",)
    assert not (tmp_path / "fixture.py").exists()


@pytest.mark.asyncio
async def test_validation_supports_negative_fixtures_without_false_success():
    engine = SynthesisEngine()
    cap = engine.synthesize(
        name="negative_fixture",
        description="proves a malformed input fails",
        code="def run(args):\n    return args['required']\n",
        input_schema={"type": "object"},
        task_id="task-negative",
    )
    result = await engine.validate(
        cap,
        [
            {
                "args": {},
                "expect_failure": True,
                "expect_error_contains": "KeyError",
            }
        ],
    )
    assert result.validation["all_passed"] is True, json.dumps(
        result.validation, indent=2, default=str
    )


@pytest.mark.asyncio
async def test_synthesis_and_generated_execution_are_dispatcher_integrated(tmp_path):
    (tmp_path / "input.txt").write_text("integrated result\n", encoding="utf-8")
    registry = CapabilityRegistry()
    fabric = CapabilityFabric(registry)
    registry.register(FilesystemCapability())
    engine = SynthesisEngine()
    registry.register(SynthesisCapability(engine, fabric))
    dispatcher = CapabilityDispatcher(
        registry,
        PolicyEngine("offline"),
        fabric=fabric,
    )
    engine.bind_dispatcher(dispatcher)
    workspace = WorkspaceSpec(id="repo", root=str(tmp_path))

    created = await dispatcher.dispatch(
        CapabilityRequest(
            capability_id="synthesis",
            arguments={
                "operation": "create",
                "name": "integrated_reader",
                "description": "reads through a governed native capability",
                "code": (
                    "def run(args):\n"
                    "    return {'text': athena.call('fs', {'operation': 'read', 'path': args['path']})}\n"
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
                "validation_cases": [{"args": {"path": "input.txt"}}],
            },
            task_id="task-integrated",
            call_id="create-integrated",
            origin=CapabilityRequestOrigin.USER_DIRECT,
        ),
        workspace=workspace,
    )
    assert created.status is CapabilityResultStatus.OK
    capability_id = json.loads(created.output)["capability_id"]

    live = await dispatcher.dispatch(
        CapabilityRequest(
            capability_id=capability_id,
            arguments={"path": "input.txt"},
            task_id="task-integrated",
            call_id="invoke-integrated",
            origin=CapabilityRequestOrigin.USER_DIRECT,
        ),
        workspace=workspace,
    )
    assert live.status is CapabilityResultStatus.OK
    assert json.loads(live.output) == {"text": "integrated result\n"}


@pytest.mark.asyncio
async def test_generated_host_mutation_stays_in_task_candidate(tmp_path):
    (tmp_path / "input.txt").write_text("before\n", encoding="utf-8")
    registry = CapabilityRegistry()
    registry.register(FilesystemCapability())
    dispatcher = CapabilityDispatcher(registry, PolicyEngine("offline"))
    shadow = ShadowEngine(
        roots_parent=str(tmp_path.parent / "generated-shadows"),
        state_root=str(tmp_path.parent / "generated-state"),
    )
    gate = RealityGate(shadow)
    shadow.bind(dispatcher)
    dispatcher.set_reality_gate(gate)
    engine = SynthesisEngine(dispatcher=dispatcher)
    cap = engine.synthesize(
        name="native_writer",
        description="writes through a governed native capability",
        code=(
            "def run(args):\n"
            "    return {'status': athena.call('fs', {'operation': 'write', 'path': args['path'], 'content': args['content']})}\n"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        effects={"READ_LOCAL", "WRITE_LOCAL"},
        required_capabilities=("fs",),
        task_id="task-generated-write",
    )
    workspace = WorkspaceSpec(
        id="repo",
        root=str(tmp_path),
        mutation_mode=MutationMode.SPECULATIVE,
    )
    validated = await engine.validate(
        cap,
        [{"args": {"path": "input.txt", "content": "validated\n"}}],
        workspace_root=str(tmp_path),
        workspace=workspace,
        task_id="task-generated-write",
        profile="autonomous",
    )
    assert validated.validation["all_passed"] is True, json.dumps(
        validated.validation, indent=2, default=str
    )
    assert engine.register_ephemeral(registry, validated) is True

    live = await dispatcher.dispatch(
        CapabilityRequest(
            capability_id=validated.id,
            arguments={"path": "input.txt", "content": "candidate\n"},
            task_id="task-generated-write",
            call_id="generated-write-call",
        ),
        workspace=workspace,
        profile="autonomous",
    )
    assert live.status is CapabilityResultStatus.OK
    assert (tmp_path / "input.txt").read_text(encoding="utf-8") == "before\n"
    branch = gate.active_branch("task-generated-write")
    assert branch is not None
    assert (Path(branch.shadow_workspace.root) / "input.txt").read_text(
        encoding="utf-8"
    ) == "candidate\n"

    await shadow.discard(branch, reason="test cleanup")
    await gate.deactivate_branch("task-generated-write")


@pytest.mark.asyncio
@pytest.mark.athena_scenario("SYNTH-001")
async def test_validate_catches_failing_case():
    engine = SynthesisEngine()
    cap = _make_cap(engine, code=BAD_CODE, name="broken")
    result = await engine.validate(cap, [{"args": {}}])
    assert result.validation["all_passed"] is False
    assert result.validation["cases_passed"] == 0


@pytest.mark.asyncio
async def test_required_dependencies_are_importable_only_from_verified_lock(tmp_path):
    environment = _write_locked_dependency(tmp_path)
    engine = SynthesisEngine()
    cap = engine.synthesize(
        name="dependency_helper",
        description="uses a locked workspace dependency",
        code=("import demo_dep\n\ndef run(args):\n    return {'value': demo_dep.VALUE}\n"),
        input_schema={"type": "object", "additionalProperties": False},
        required_dependencies=(DependencyRequirement("demo-dep", version="1.2.3"),),
        task_id="task-deps",
    )

    validated = await engine.validate(
        cap,
        [{"args": {}, "expect_output_contains": "from locked dependency"}],
        workspace_root=str(tmp_path),
    )

    if not validated.validation["all_passed"]:
        raise AssertionError(json.dumps(validated.validation, indent=2, default=str))
    assert validated.dependency_lock["environment_fingerprint"] == environment
    registry = CapabilityRegistry()
    assert engine.register_ephemeral(registry, validated) is True
    result = await registry.executor_for("synth_dependency_helper").invoke(
        CapabilityRequest(
            capability_id="synth_dependency_helper",
            arguments={},
            task_id="task-deps",
            call_id="call-deps",
        ),
        context=SimpleNamespace(workspace=WorkspaceSpec(id="repo", root=str(tmp_path))),
    )
    assert result.status is CapabilityResultStatus.OK
    assert json.loads(result.output) == {"value": "from locked dependency"}


@pytest.mark.asyncio
async def test_required_dependency_without_lock_is_rejected(tmp_path):
    target = tmp_path / ".athena" / "dependencies"
    (target / "demo_dep").mkdir(parents=True)
    (target / "demo_dep" / "__init__.py").write_text("VALUE = 'unlocked'\n")
    engine = SynthesisEngine()
    cap = engine.synthesize(
        name="unlocked_helper",
        description="must not use an unrecorded import",
        code="import demo_dep\ndef run(args):\n    return demo_dep.VALUE\n",
        required_dependencies=(DependencyRequirement("demo-dep"),),
    )

    rejected = await engine.validate(cap, [{"args": {}}], workspace_root=str(tmp_path))

    assert rejected.validation["all_passed"] is False
    assert rejected.validation["details"][0]["case"] == "dependencies"
    assert "lock" in rejected.validation["details"][0]["error"]


def test_restore_rejects_changed_dependency_environment(tmp_path):
    _write_locked_dependency(tmp_path)
    engine = SynthesisEngine()
    generated = engine._generated_record(
        engine.synthesize(
            name="restore_dependency",
            description="restore check",
            code="import demo_dep\n\n\ndef run(args):\n    return demo_dep.VALUE\n",
            required_dependencies=(DependencyRequirement("demo-dep", version="1.2.3"),),
        ),
        scope=AffordanceScope.PROJECT,
        project_scope="repo",
    )
    generated = GeneratedCapability.from_record(
        {
            **generated.to_record(),
            "dependency_lock": {
                **dict(generated.dependency_lock),
                "environment_fingerprint": "changed",
            },
        }
    )

    with pytest.raises(ValueError, match="fingerprint"):
        engine.restore_executor(generated, workspace_root=str(tmp_path))


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

    assert cap.effects == frozenset(
        {
            "READ_LOCAL",
            "WRITE_LOCAL",
            "NETWORK_WRITE",
            "PRIVILEGED",
        }
    )
    assert cap.effective_effects == frozenset(
        {
            EffectClass.READ_LOCAL.value,
            EffectClass.EXECUTE.value,
        }
    )


def test_generated_record_carries_reproducible_dependency_lock():
    engine = SynthesisEngine()
    cap = engine.synthesize(
        name="locked_helper",
        description="uses an explicitly recorded dependency set",
        code=GOOD_CODE,
        required_dependencies=(DependencyRequirement("httpx", version="0.28.1", reason="fetch"),),
    )

    generated = engine._generated_record(cap, scope=AffordanceScope.PROJECT, project_scope="repo")
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
        capability_id=cap.id, arguments={"msg": "hello"}, task_id="task_1", call_id="call_1"
    )
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
        await executor.invoke(
            CapabilityRequest(capability_id=cap.id, arguments={}, task_id="task_1", call_id=f"c{i}")
        )
    proof = engine.proof_for(cap.id)
    assert proof["uses"] == 2
    assert proof["successes"] == 2


@pytest.mark.asyncio
@pytest.mark.athena_scenario("SYNTH-004")
async def test_to_skill_candidate_requires_diverse_repeated_success():
    engine = SynthesisEngine()
    registry = CapabilityRegistry()
    cap = _make_cap(engine)
    await engine.validate(cap, [{"args": {}}])
    engine.register_ephemeral(registry, cap)

    assert engine.to_skill_candidate(cap.id) is None  # zero uses

    executor = registry.executor_for(cap.id)
    await executor.invoke(
        CapabilityRequest(
            capability_id=cap.id, arguments={"msg": "one"}, task_id="task_1", call_id="c1"
        )
    )
    assert engine.to_skill_candidate(cap.id) is None  # only one use

    await executor.invoke(
        CapabilityRequest(
            capability_id=cap.id, arguments={"msg": "two"}, task_id="task_1", call_id="c2"
        )
    )
    assert engine.to_skill_candidate(cap.id) is None  # two uses are insufficient

    await executor.invoke(
        CapabilityRequest(
            capability_id=cap.id, arguments={"msg": "three"}, task_id="task_1", call_id="c3"
        )
    )
    candidate = engine.to_skill_candidate(cap.id)
    assert candidate is not None
    assert candidate.draft.name == "greeter"
    assert len(candidate.evidence) == 2
