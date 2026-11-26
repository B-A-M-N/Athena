from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from athena.capabilities.dispatcher import CapabilityDispatcher
from athena.capabilities.fs import FilesystemCapability
from athena.capabilities.registry import CapabilityRegistry
from athena.capabilities.workflow import WorkflowCapability
from athena.capabilities.workflow_workspace import cancel_safe_cleanup
from athena.policy.engine import PolicyEngine
from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
    ExternalEffectPhase,
)
from athena.protocol.tasks import AutonomyLevel, NetworkPolicy, WorkspaceSpec
from athena.state.external_effects import ExternalEffectStore
from athena.workflows.models import Workflow, WorkflowStep


class _Store:
    def __init__(self, workflow: Workflow):
        self.workflow = workflow
        self.promoted = None

    async def get(self, workflow_id, **kwargs):
        del kwargs
        return self.workflow if workflow_id == self.workflow.id else None

    async def promote_candidate(self, workflow_id, **kwargs):
        assert workflow_id == self.workflow.id
        self.promoted = kwargs
        self.workflow = Workflow.from_record(
            {
                **self.workflow.to_record(),
                "scope": kwargs["scope"],
                "task_scope": None,
                "project_scope": kwargs.get("project_id"),
                "user_scope": kwargs.get("user_id"),
                "lifecycle_state": "PROMOTED",
                "provenance": {
                    **self.workflow.provenance,
                    "promotion_replay_validation": dict(kwargs["validation"]),
                },
            }
        )
        return self.workflow


class _Fabric:
    def executor_for(self, capability_id, **kwargs):
        del kwargs
        return SimpleNamespace(
            descriptor=CapabilityDescriptor(
                id=capability_id,
                description=capability_id,
                input_schema={"type": "object"},
                effects=frozenset({EffectClass.READ_LOCAL}),
            )
        )


class _NetworkFabric:
    def executor_for(self, capability_id, **kwargs):
        del kwargs
        return SimpleNamespace(
            descriptor=CapabilityDescriptor(
                id=capability_id,
                description=capability_id,
                input_schema={"type": "object"},
                effects=frozenset({EffectClass.NETWORK_WRITE}),
            )
        )


class _Dispatcher:
    async def dispatch(self, request, **kwargs):
        del kwargs
        return CapabilityResult(
            request.call_id,
            request.capability_id,
            CapabilityResultStatus.OK,
            output=json.dumps({"ok": True}),
        )


class _FailingDispatcher:
    async def dispatch(self, request, **kwargs):
        del kwargs
        return CapabilityResult(
            request.call_id,
            request.capability_id,
            CapabilityResultStatus.FAILED,
            error="step failed",
        )


@pytest.mark.asyncio
async def test_workflow_effects_are_resolved_from_owned_graph(tmp_path):
    from athena.workflows.models import WorkflowStep

    workflow = Workflow.create(
        name="offline-read",
        description="read one local file",
        steps=(
            WorkflowStep(
                id="read",
                capability_id="fs",
                arguments={"operation": "read", "path": "example.txt"},
            ),
        ),
        task_scope="task-offline",
    )
    capability = WorkflowCapability(_Store(workflow), _Dispatcher(), _Fabric())
    effects = await capability.resolve_operation_effects(
        {"operation": "run", "workflow_id": workflow.id},
        workspace=WorkspaceSpec(id="repo", root=str(tmp_path), network_policy=NetworkPolicy.DENY),
        task_id="task-offline",
        principal_id="agent",
    )
    assert effects == (EffectClass.READ_LOCAL,)

    network_workflow = Workflow.create(
        name="network",
        description="write to a remote service",
        steps=(WorkflowStep(id="send", capability_id="network", arguments={}),),
        task_scope="task-offline",
    )
    network_capability = WorkflowCapability(
        _Store(network_workflow), _Dispatcher(), _NetworkFabric()
    )
    network_effects = await network_capability.resolve_operation_effects(
        {"operation": "run", "workflow_id": network_workflow.id},
        workspace=WorkspaceSpec(id="repo", root=str(tmp_path), network_policy=NetworkPolicy.DENY),
        task_id="task-offline",
        principal_id="agent",
    )
    assert network_effects == (EffectClass.NETWORK_WRITE,)


@pytest.mark.asyncio
async def test_real_dispatcher_allows_local_workflow_in_offline_workspace(tmp_path):
    from athena.affordances import CapabilityFabric
    from athena.workflows.models import WorkflowStep

    (tmp_path / "input.txt").write_text("offline", encoding="utf-8")
    workflow = Workflow.create(
        name="offline-read",
        description="read one local file",
        steps=(
            WorkflowStep(
                id="read",
                capability_id="fs",
                arguments={"operation": "read", "path": "input.txt"},
            ),
        ),
        task_scope="task-offline-dispatch",
    )
    registry = CapabilityRegistry()
    registry.register(FilesystemCapability())
    fabric = CapabilityFabric(registry)

    class _DispatcherBridge:
        target = None

        async def dispatch(self, request, **kwargs):
            return await self.target.dispatch(request, **kwargs)

    bridge = _DispatcherBridge()
    capability = WorkflowCapability(_Store(workflow), bridge, fabric)
    registry.register(capability)
    bridge.target = CapabilityDispatcher(registry, PolicyEngine(AutonomyLevel.OFFLINE))

    result = await bridge.target.dispatch(
        CapabilityRequest(
            capability_id="workflow",
            task_id="task-offline-dispatch",
            call_id="offline-run",
            arguments={"operation": "run", "workflow_id": workflow.id},
        ),
        workspace=WorkspaceSpec(
            id="repo",
            root=str(tmp_path),
            network_policy=NetworkPolicy.DENY,
        ),
    )

    assert result.status is CapabilityResultStatus.OK, result.error
    assert json.loads(result.output)["status"] == "completed"


@pytest.mark.asyncio
async def test_real_dispatcher_denies_network_workflow_offline(tmp_path):
    from athena.affordances import CapabilityFabric

    class _NetworkExecutor:
        descriptor = CapabilityDescriptor(
            id="net-write",
            description="network write fixture",
            input_schema={"type": "object"},
            effects=frozenset({EffectClass.NETWORK_WRITE}),
        )

        def __init__(self):
            self.calls = 0

        async def invoke(self, request, **kwargs):
            del request, kwargs
            self.calls += 1
            return CapabilityResult(
                "network-call",
                "net-write",
                CapabilityResultStatus.OK,
            )

    network = _NetworkExecutor()
    workflow = Workflow.create(
        name="offline-network",
        description="attempt a remote write",
        steps=(WorkflowStep(id="send", capability_id="net-write", arguments={}),),
        task_scope="task-network-dispatch",
    )
    registry = CapabilityRegistry()
    registry.register(network)
    fabric = CapabilityFabric(registry)

    class _DispatcherBridge:
        target = None

        async def dispatch(self, request, **kwargs):
            return await self.target.dispatch(request, **kwargs)

    bridge = _DispatcherBridge()
    capability = WorkflowCapability(_Store(workflow), bridge, fabric)
    registry.register(capability)
    bridge.target = CapabilityDispatcher(registry, PolicyEngine(AutonomyLevel.OFFLINE))

    result = await bridge.target.dispatch(
        CapabilityRequest(
            capability_id="workflow",
            task_id="task-network-dispatch",
            call_id="network-run",
            arguments={"operation": "run", "workflow_id": workflow.id},
        ),
        workspace=WorkspaceSpec(
            id="repo",
            root=str(tmp_path),
            network_policy=NetworkPolicy.DENY,
        ),
    )

    assert result.status is CapabilityResultStatus.FAILED
    assert "network" in (result.error or "").lower()
    assert network.calls == 0


class _RecoveryRunStore:
    def __init__(self):
        self.calls = []

    async def reconcile_external_effect(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return {"status": "resumed", "run_id": args[0]}


@pytest.mark.asyncio
async def test_workflow_promotion_replays_candidate_and_persists_proof(tmp_path):
    from athena.affordances.models import AffordanceScope
    from athena.workflows.models import WorkflowStep

    workflow = Workflow.create(
        name="candidate",
        description="candidate",
        steps=(
            WorkflowStep(
                id="one",
                capability_id="fs",
                arguments={"operation": "read", "path": "example.txt"},
            ),
        ),
        scope=AffordanceScope.CANDIDATE,
        task_scope="task-1",
        provenance={
            "successful_observations": 2,
            "observed_task_ids": ["task-1", "task-2"],
        },
    )
    store = _Store(workflow)
    capability = WorkflowCapability(store, _Dispatcher(), _Fabric())
    result = await capability.invoke(
        CapabilityRequest(
            capability_id="workflow",
            arguments={
                "operation": "promote",
                "workflow_id": workflow.id,
                "scope": "project",
            },
            task_id="task-1",
            call_id="promote-1",
        ),
        context=SimpleNamespace(
            workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
        ),
    )

    assert result.status is CapabilityResultStatus.OK
    assert store.promoted is not None
    assert store.promoted["validation"]["status"] == "completed"
    assert store.workflow.provenance["promotion_replay_validation"]["status"] == "completed"


@pytest.mark.asyncio
async def test_workflow_promotion_requires_distinct_observations(tmp_path):
    from athena.affordances.models import AffordanceScope
    from athena.workflows.models import WorkflowStep

    workflow = Workflow.create(
        name="candidate",
        description="candidate",
        steps=(
            WorkflowStep(
                id="one",
                capability_id="fs",
                arguments={"operation": "read", "path": "example.txt"},
            ),
        ),
        scope=AffordanceScope.CANDIDATE,
        task_scope="task-1",
        provenance={
            "origin": "successful_workflow_execution",
            "successful_observations": 1,
            "observed_task_ids": ["task-1"],
        },
    )
    store = _Store(workflow)
    capability = WorkflowCapability(store, _Dispatcher(), _Fabric())

    result = await capability.invoke(
        CapabilityRequest(
            capability_id="workflow",
            arguments={
                "operation": "promote",
                "workflow_id": workflow.id,
                "scope": "project",
            },
            task_id="task-1",
            call_id="promote-1",
        ),
        context=SimpleNamespace(
            workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
        ),
    )

    assert result.status is CapabilityResultStatus.FAILED
    assert "two distinct" in (result.error or "")
    assert store.promoted is None


@pytest.mark.asyncio
async def test_workflow_recovery_uses_durable_external_receipt():
    external = ExternalEffectStore()
    await external.prepare(
        transaction_id="tx-recover",
        task_id="task-1",
        capability_id="network",
        external_identity="POST https://example.test/items",
        request_digest="digest",
        idempotency_key="key",
        phase=ExternalEffectPhase.PREPARE,
    )
    receipt = await external.finish("tx-recover", status="VERIFIED")
    run_store = _RecoveryRunStore()
    workflow = Workflow.create(
        name="recovery",
        description="recovery",
        steps=(),
    )
    capability = WorkflowCapability(
        _Store(workflow),
        _Dispatcher(),
        _Fabric(),
        run_store=run_store,
        external_store=external,
    )

    result = await capability.invoke(
        CapabilityRequest(
            capability_id="workflow",
            task_id="task-1",
            call_id="recover-1",
            arguments={
                "operation": "recover",
                "workflow_id": workflow.id,
                "run_id": "run-1",
                "step_id": "write",
                "item_index": 0,
                "transaction_id": "tx-recover",
                "resolution": "resume",
            },
        ),
        context=SimpleNamespace(workspace=WorkspaceSpec(id="repo", root="/tmp")),
    )

    assert result.status is CapabilityResultStatus.OK
    assert json.loads(result.output)["status"] == "resumed"
    assert run_store.calls[0][1]["receipt"] == receipt


@pytest.mark.asyncio
async def test_workflow_recovery_uses_canonical_dispatcher_contract(tmp_path):
    external = ExternalEffectStore()
    await external.prepare(
        transaction_id="tx-dispatch",
        task_id="task-dispatch",
        capability_id="network",
        external_identity="POST https://example.test/items",
        request_digest="digest",
        idempotency_key="key",
        phase=ExternalEffectPhase.PREPARE,
    )
    receipt = await external.finish("tx-dispatch", status="VERIFIED")
    run_store = _RecoveryRunStore()
    workflow = Workflow.create(name="recovery", description="recovery", steps=())
    capability = WorkflowCapability(
        _Store(workflow),
        _Dispatcher(),
        _Fabric(),
        run_store=run_store,
        external_store=external,
    )
    registry = CapabilityRegistry()
    registry.register(capability)
    dispatcher = CapabilityDispatcher(
        registry,
        PolicyEngine(AutonomyLevel.AUTONOMOUS),
    )
    request = CapabilityRequest(
        capability_id="workflow",
        task_id="task-dispatch",
        call_id="recover-dispatch-1",
        arguments={
            "operation": "recover",
            "workflow_id": workflow.id,
            "run_id": "run-dispatch",
            "step_id": "write",
            "item_index": 0,
            "transaction_id": "tx-dispatch",
            "resolution": "resume",
        },
    )

    result = await dispatcher.dispatch(
        request,
        workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
    )

    assert result.status is CapabilityResultStatus.OK
    assert json.loads(result.output)["status"] == "resumed"
    assert run_store.calls[0][1]["receipt"] == receipt


@pytest.mark.asyncio
async def test_completed_workflow_notifies_learning_observer(tmp_path):
    from athena.workflows.models import WorkflowStep

    workflow = Workflow.create(
        name="observed",
        description="observed",
        steps=(
            WorkflowStep(
                id="read",
                capability_id="fs",
                arguments={"operation": "read"},
            ),
        ),
    )
    observed = []

    async def observer(**kwargs):
        observed.append(kwargs)

    capability = WorkflowCapability(
        _Store(workflow),
        _Dispatcher(),
        _Fabric(),
        workflow_observer=observer,
    )
    result = await capability.invoke(
        CapabilityRequest(
            capability_id="workflow",
            task_id="task-observed",
            call_id="run-1",
            arguments={"operation": "run", "workflow_id": workflow.id},
        ),
        context=SimpleNamespace(
            workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
        ),
    )

    assert result.status is CapabilityResultStatus.OK
    assert observed and observed[0]["task_id"] == "task-observed"
    assert observed[0]["workflow"].id == workflow.id


@pytest.mark.asyncio
async def test_workflow_trial_copy_does_not_block_unrelated_stream(tmp_path):
    from athena.workflows.models import WorkflowStep

    for index in range(2_000):
        (tmp_path / f"source-{index:04d}.txt").write_bytes(b"x" * 1024)
    workflow = Workflow.create(
        name="large trial",
        description="stage a sizeable disposable workspace",
        steps=(
            WorkflowStep(
                id="read",
                capability_id="fs",
                arguments={"operation": "read"},
            ),
        ),
    )
    capability = WorkflowCapability(
        _Store(workflow),
        _Dispatcher(),
        _Fabric(),
        max_trial_files=3_000,
        max_trial_bytes=4 * 1024 * 1024,
    )
    task = asyncio.create_task(
        capability.invoke(
            CapabilityRequest(
                capability_id="workflow",
                task_id="task-trial",
                call_id="trial-1",
                arguments={"operation": "trial", "workflow_id": workflow.id},
            ),
            context=SimpleNamespace(
                workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
            ),
        )
    )
    ticks = 0
    while not task.done():
        ticks += 1
        await asyncio.sleep(0)

    result = await task
    assert result.status is CapabilityResultStatus.OK
    assert ticks > 10


@pytest.mark.asyncio
async def test_disposable_workspace_cleanup_survives_cancellation(tmp_path):
    disposable = tmp_path / "disposable"
    disposable.mkdir()
    for index in range(2_000):
        (disposable / f"artifact-{index:04d}").write_bytes(b"x")

    cleanup = asyncio.create_task(cancel_safe_cleanup(str(disposable)))
    await asyncio.sleep(0)
    cleanup.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cleanup

    assert not disposable.exists()


@pytest.mark.asyncio
async def test_failed_workflow_does_not_notify_learning_observer(tmp_path):
    from athena.workflows.models import WorkflowStep

    workflow = Workflow.create(
        name="failed",
        description="failed",
        steps=(
            WorkflowStep(
                id="read",
                capability_id="fs",
                arguments={"operation": "read"},
            ),
        ),
    )
    observed = []

    async def observer(**kwargs):
        observed.append(kwargs)

    capability = WorkflowCapability(
        _Store(workflow),
        _FailingDispatcher(),
        _Fabric(),
        workflow_observer=observer,
    )
    result = await capability.invoke(
        CapabilityRequest(
            capability_id="workflow",
            task_id="task-failed",
            call_id="run-failed",
            arguments={"operation": "run", "workflow_id": workflow.id},
        ),
        context=SimpleNamespace(
            workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
        ),
    )

    assert result.status is CapabilityResultStatus.FAILED
    assert observed == []
