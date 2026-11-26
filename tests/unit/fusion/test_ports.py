"""Typed Fusion ports remove whole-service reach-through and verify candidates."""

from __future__ import annotations

from types import SimpleNamespace


from athena.fusion import FusionOrchestrator
from athena.fusion.ports import FusionPorts, TaskForkPorts


class _Verifier:
    async def verify_candidate(self, task, *, workspace, **_):
        return [{"id": "typed-proof", "passed": True}]

    async def impact_for(self, root, changed_resources):
        return {}


class _Gate:
    def active_branch(self, task_id):
        return None

    async def deactivate_branch(self, task_id):
        return None


def test_typed_ports_are_stored_and_service_is_not_required():
    shadow = SimpleNamespace(_state_root="")
    checkpoints = SimpleNamespace()
    task_store = SimpleNamespace()
    verifier = _Verifier()
    gate = _Gate()
    orchestrator = FusionOrchestrator(
        typed_ports=FusionPorts(
            shadow=shadow,
            checkpoints=checkpoints,
            task_store=task_store,
            candidate_verifier=verifier,
            reality_gate=gate,
            fork=TaskForkPorts(
                task_store=task_store,
                task_manager=SimpleNamespace(),
                event_store=SimpleNamespace(),
                session_store=SimpleNamespace(),
                checkpoint_manager=checkpoints,
            ),
        )
    )
    assert orchestrator.ports.candidate_verifier is verifier
    assert orchestrator.ports.reality_gate is gate
    assert orchestrator.service is None
    assert orchestrator._store_tasks is task_store


def test_task_fork_ports_supports_service_free_construction():
    ports = TaskForkPorts(
        task_store=SimpleNamespace(),
        task_manager=SimpleNamespace(),
        event_store=SimpleNamespace(),
        session_store=SimpleNamespace(),
        checkpoint_manager=SimpleNamespace(),
    )
    assert ports.task_store is not None


def test_typed_ports_supply_semantic_snapshot_resources(tmp_path):
    from athena.fusion.semantic_snapshot import SemanticSnapshot
    from athena.fusion.ports import FusionPorts
    from athena.fusion.orchestrator import FusionOrchestrator

    shadow = SimpleNamespace(_state_root=str(tmp_path), list_branches=lambda: ())
    task_store = SimpleNamespace()
    event_store = SimpleNamespace()
    orchestrator = FusionOrchestrator(
        typed_ports=FusionPorts(
            shadow=shadow,
            checkpoints=SimpleNamespace(),
            task_store=task_store,
            event_store=event_store,
            fork=TaskForkPorts(
                task_store=task_store,
                task_manager=SimpleNamespace(),
                event_store=event_store,
                session_store=SimpleNamespace(),
            ),
        )
    )
    assert isinstance(orchestrator._semantic_state, SemanticSnapshot)
    assert orchestrator._semantic_state._task_store is task_store
    assert orchestrator._semantic_state._event_store is event_store
