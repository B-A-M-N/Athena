"""Execution graph acquisition for service startup."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from athena.execution.container import ContainerBackend
from athena.execution.manager import ExecutionManager
from athena.execution.runtimes import PythonRuntime, ShellRuntime
from athena.execution.runtimes.node import NodeRuntime
from athena.execution.runtimes.powershell import PowerShellRuntime
from athena.execution.runtime_host import LocalRuntimeSupervisor, SupervisedLocalBackend
from athena.execution.ssh import SSHBackend
from athena.execution.ssh_profile import SSHProfile
from athena.state.context_blocks import ContextBlockStore
from athena.state.database import Database
from athena.state.delegate_sessions import DelegateSessionStore
from athena.state.executions import ExecutionStore
from athena.state.runtime_sessions import RuntimeSessionStore
from athena.state.self_host import SelfHostMissionStore
from athena.state.tool_repairs import ToolRepairStore

_logger = logging.getLogger("athena.service")


@dataclass
class ExecutionComponents:
    """Explicit outputs of execution and runtime support acquisition."""

    execution: ExecutionManager
    runtime_sessions: RuntimeSessionStore
    execution_store: ExecutionStore
    runtime_host_supervisor: Any
    self_host_missions: SelfHostMissionStore
    tool_repair_store: ToolRepairStore
    context_block_store: ContextBlockStore
    pack_store: Any
    pack_hook_outbox: Any
    pack_manager: Any
    delegate_session_store: DelegateSessionStore
    capability_health_store: Any
    capability_health: Any


async def build_execution_components(
    *,
    db: Database,
    config: Any,
    runtime_state_root: str,
    event_sink: Any,
    secret_manager: Any,
    startup_health: dict[str, Any],
) -> ExecutionComponents:
    """Construct execution backends, runtimes, and execution support stores."""
    runtime_sessions = RuntimeSessionStore(db)
    execution_store = ExecutionStore(db)
    execution = ExecutionManager(
        runtime_session_store=runtime_sessions,
        execution_store=execution_store,
        event_sink=event_sink,
        durability_mandatory=True,
    )
    runtime_host_supervisor = None
    if config.local_runtime_supervisor:
        runtime_host_supervisor = LocalRuntimeSupervisor(
            os.path.join(str(runtime_state_root), "runtime-host")
        )
        execution.set_local_backend(SupervisedLocalBackend(runtime_host_supervisor))

    execution.register_backend(ContainerBackend())
    for backend_name, raw_profile in config.execution_backends.items():
        if str(raw_profile.get("kind", "ssh")).casefold() != "ssh":
            _logger.warning("unsupported execution backend kind for %s", backend_name)
            continue
        try:
            profile = SSHProfile(
                name=str(backend_name),
                host=str(raw_profile["host"]),
                user=str(raw_profile["user"]),
                port=int(raw_profile.get("port", 22)),
                credential_id=(
                    str(raw_profile["credential_id"]) if raw_profile.get("credential_id") else None
                ),
                identity_file=(
                    str(raw_profile["identity_file"]) if raw_profile.get("identity_file") else None
                ),
                known_hosts=str(raw_profile["known_hosts"]),
                remote_root=str(raw_profile.get("remote_root", "~/athena-workspaces")),
                connect_timeout=float(raw_profile.get("connect_timeout", 15.0)),
            )
            execution.register_backend(SSHBackend(profile, secret_manager=secret_manager))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"invalid SSH execution backend profile {backend_name!r}: {exc}"
            ) from exc

    execution.register_runtime(PythonRuntime())
    execution.register_runtime(ShellRuntime())
    if PowerShellRuntime.available():
        execution.register_runtime(PowerShellRuntime())
    if NodeRuntime.available():
        execution.register_runtime(NodeRuntime())

    self_host_missions = SelfHostMissionStore(db)
    tool_repair_store = ToolRepairStore(db)
    context_block_store = ContextBlockStore(db)
    pack_store = _pack_store(db)
    pack_hook_outbox = _pack_hook_outbox(db)
    pack_manager = _pack_manager(pack_store, runtime_state_root)
    delegate_session_store = DelegateSessionStore(db)
    capability_health_store = _capability_health_store(db)
    capability_health = _capability_health(capability_health_store)
    try:
        await capability_health.load(await capability_health_store.list())
        startup_health["checks"]["capability_health"] = {"status": "ok", "blocking": False}
    except Exception as exc:
        _logger.warning("capability health rehydration failed: %s", exc)
        startup_health["checks"]["capability_health"] = {
            "status": "degraded",
            "blocking": False,
            "error": str(exc),
        }

    return ExecutionComponents(
        execution=execution,
        runtime_sessions=runtime_sessions,
        execution_store=execution_store,
        runtime_host_supervisor=runtime_host_supervisor,
        self_host_missions=self_host_missions,
        tool_repair_store=tool_repair_store,
        context_block_store=context_block_store,
        pack_store=pack_store,
        pack_hook_outbox=pack_hook_outbox,
        pack_manager=pack_manager,
        delegate_session_store=delegate_session_store,
        capability_health_store=capability_health_store,
        capability_health=capability_health,
    )


def _pack_store(db: Database) -> Any:
    from athena.packs.store import PackStore

    return PackStore(db)


def _pack_hook_outbox(db: Database) -> Any:
    from athena.state.pack_hooks import PackHookOutbox

    return PackHookOutbox(db)


def _pack_manager(pack_store: Any, runtime_state_root: str) -> Any:
    from athena.packs.manager import PackManager

    return PackManager(pack_store, install_root=os.path.join(runtime_state_root, "packs"))


def _capability_health_store(db: Database) -> Any:
    from athena.state.capability_health import CapabilityHealthStore

    return CapabilityHealthStore(db)


def _capability_health(store: Any) -> Any:
    from athena.capabilities.health import CapabilityHealth

    return CapabilityHealth(store=store)


__all__ = ["ExecutionComponents", "build_execution_components"]
