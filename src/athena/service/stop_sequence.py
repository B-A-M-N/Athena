"""Service stop sequence helpers.

The lifecycle retains stop ordering and unwind authority; these collaborators
isolate process teardown, transport shutdown, and durable-store release so each
failure mode remains independently observable.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from athena.protocol.events import EV, make_event
from athena.protocol.tasks import TaskStatus
from athena.service.blocking_shutdown import shutdown_blocking_workers

_logger = logging.getLogger("athena.service")


async def stop(ports: Any) -> None:
    if not ports.started and ports.db is None:
        return

    if ports.worker_task is not None:
        if ports.worker is not None:
            try:
                await ports.worker.stop()
            except Exception as exc:
                _logger.warning("worker stop failed: %s", exc)
        try:
            await ports.worker_task
        except Exception as exc:
            _logger.warning("worker task teardown failed: %s", exc)
        ports.worker_task = None
    recovery_tasks = list(getattr(ports, "approval_recovery_tasks", ()))
    for recovery in recovery_tasks:
        recovery.cancel()
    if recovery_tasks:
        await asyncio.gather(*recovery_tasks, return_exceptions=True)
    ports.approval_recovery_tasks.clear()

    if ports.scheduler is not None:
        try:
            await ports.scheduler.stop()
        except Exception as exc:
            _logger.warning("scheduler stop failed: %s", exc)
        if ports.store_events is not None:
            ports.store_events.unsubscribe(ports.scheduler.notify_event)
        ports.scheduler = None

    if ports.store_events is not None and ports.synthesis_event_observer is not None:
        ports.store_events.unsubscribe(ports.synthesis_event_observer)
        ports.synthesis_event_observer = None
    for callback in getattr(ports, "observation_callbacks", None) or []:
        if ports.store_events is not None:
            ports.store_events.unsubscribe(callback)
    ports.observation_callbacks = []

    if ports.store_tasks is not None and ports.task_manager is not None:
        try:
            rows = await ports.store_tasks.list_by_status(TaskStatus.RUNNING)
            for row in rows or []:
                tid = row.get("id") if isinstance(row, dict) else getattr(row, "id", None)
                if not tid:
                    continue
                if ports.execution is not None:
                    try:
                        await ports.execution.cancel_task(tid)
                    except Exception as exc:
                        _logger.warning("cancel task %s on stop failed: %s", tid, exc)
                try:
                    await ports.task_manager.transition(
                        tid, TaskStatus.INTERRUPTED, reason="service stopping"
                    )
                except Exception as exc:
                    _logger.warning("interrupt task %s on stop failed: %s", tid, exc)
        except Exception as exc:
            _logger.warning("interrupt-running-tasks on stop failed: %s", exc)

    poll_task = getattr(ports, "watch_poll_task", None)
    if poll_task is not None:
        poll_task.cancel()
        try:
            await poll_task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            _logger.warning("watch poller teardown failed: %s", exc)
        ports.watch_poll_task = None

    if ports.resource_finalizer is not None:
        try:
            await ports.resource_finalizer.shutdown()
        except Exception as exc:
            _logger.warning("parked-resource retention teardown failed: %s", exc)

    hook_outcome = await ports.run_shutdown_hooks()
    ports.computer = None
    ports.browser = None
    ports.terminals = None
    ports.debugger = None
    ports.computer_health = {
        "state": "stopped",
        "backend": "unknown",
    }
    ports.browser_health = {
        "state": "stopped",
        "configured": False,
        "active_sessions": 0,
    }

    if ports.pack_manager is not None:
        await ports.pack_manager.lifecycle.stop_hook_dispatcher()
    if ports.mcp_supervisor is not None:
        await ports.mcp_supervisor.stop()
        ports.mcp_supervisor = None
    for client in ports.mcp_clients:
        try:
            await client.close()
        except Exception as exc:
            _logger.warning("MCP client close failed: %s", exc)
    ports.mcp_clients = []
    for status in ports.mcp_connection_status.values():
        status["state"] = "stopped"
        status["tool_count"] = 0

    try:
        await ports.close_hermes_referee()
    except Exception as exc:
        _logger.warning("Hermes referee close failed: %s", exc)

    execution_outcome: dict = {
        "runtime_failures": [],
        "backend_failures": [],
        "sessions_remaining": [],
        "unproven_process_kills": [],
    }
    if ports.execution is not None:
        try:
            outcome = await ports.execution.close_all()
            execution_outcome = dict(outcome)
        except Exception as exc:
            _logger.warning("execution close_all failed: %s", exc)
        else:
            if outcome.get("runtime_failures") or outcome.get("sessions_remaining"):
                _logger.warning(
                    "execution shutdown incomplete: %d runtime failures, %d sessions remaining",
                    len(outcome.get("runtime_failures", ())),
                    len(outcome.get("sessions_remaining", ())),
                )
        if ports.execution.live_resource_count() > 0:
            _logger.warning(
                "execution manager still holds %d live resources after close_all",
                ports.execution.live_resource_count(),
            )
        ports.execution = None

    await shutdown_blocking_workers()

    shutdown_clean = not (
        hook_outcome.get("failures")
        or execution_outcome.get("runtime_failures")
        or execution_outcome.get("backend_failures")
        or execution_outcome.get("sessions_remaining")
        or execution_outcome.get("unproven_process_kills")
        or (ports.resource_finalizer and ports.resource_finalizer.health().get("failures"))
    )
    ports.shutdown_status = {
        "state": "clean" if shutdown_clean else "incomplete",
        "hooks": hook_outcome,
        "execution": execution_outcome,
        "resources": (
            ports.resource_finalizer.health() if ports.resource_finalizer is not None else None
        ),
    }
    if not shutdown_clean and ports.store_events is not None:
        try:
            await ports.store_events.append(
                make_event(EV["SHUTDOWN_INCOMPLETE"], ports.shutdown_status)
            )
        except Exception as exc:
            _logger.warning("shutdown incomplete marker failed: %s", exc)

    if ports.db is not None:
        if ports.store_events is not None:
            try:
                await ports.store_events.close()
            except Exception as exc:
                _logger.warning("event store close failed: %s", exc)
        if ports.fabric is not None:
            try:
                await ports.fabric.flush()
            except Exception as exc:
                _logger.warning("generated capability flush failed: %s", exc)
        try:
            await ports.db.close()
        except Exception as exc:
            _logger.warning("db close failed: %s", exc)
        ports.db = None

    for attr in (
        "_cancellations",
        "_world_state_store",
        "_project_index_store",
        "_project_index_builder",
        "_project_index_coordinator",
        "_failure_memory",
        "_generated_store",
        "_workflow_store",
        "_workflow_run_store",
        "_research_store",
        "_context_block_store",
        "_pack_store",
        "_pack_manager",
        "_pack_hook_outbox",
        "_skill_lifecycle",
        "_delegate_session_store",
        "_external_delegate_manager",
        "_capability_health_store",
        "_capability_health",
        "_synthesis",
    ):
        setattr(ports, attr.removeprefix("_"), None)
    ports.world_states = {}
    ports.started = False
