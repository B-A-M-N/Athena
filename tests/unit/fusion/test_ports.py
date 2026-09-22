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
