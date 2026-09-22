"""Pack hook runtime uses explicit shared state without owner tunneling."""

from __future__ import annotations

import pytest

from athena.packs.hooks import PackHookRuntime
from athena.packs.ports import PackHookPorts
from athena.packs.runtime_state import PackRuntimeState


class _Outbox:
    async def pending(self):
        return []


def test_manager_state_and_hook_ports_share_identity():
    state = PackRuntimeState()
    ports = PackHookPorts(
        hook_callbacks=state.hooks.callbacks,
        hook_contracts=state.hooks.contracts,
        hook_health=state.hooks.health,
        hook_outbox=state.hooks.outbox,
        hook_retry_task=lambda: state.hooks.retry_task,
        workflow_store=None,
        state=state.hooks,
    )
    runtime = PackHookRuntime(ports)
    assert runtime._hooks is state.hooks
    state.hooks.health["iterations"] = 7
    assert runtime.health()["iterations"] == 7


@pytest.mark.asyncio
async def test_hook_start_uses_shared_retry_task_without_owner_mutation():
    state = PackRuntimeState()
    state.hooks.outbox = _Outbox()
    ports = PackHookPorts(
        hook_callbacks=state.hooks.callbacks,
        hook_contracts=state.hooks.contracts,
        hook_health=state.hooks.health,
        hook_outbox=state.hooks.outbox,
        hook_retry_task=lambda: state.hooks.retry_task,
        workflow_store=None,
        state=state.hooks,
    )
    runtime = PackHookRuntime(ports)
    await runtime.start(interval_s=0.1)
    task = state.hooks.retry_task
    assert task is not None
    await runtime.stop()
    assert state.hooks.retry_task is None
