"""Canonical core capability registration for the service façade.

This module owns the wiring list and optional-capability health evidence. It
never authorizes execution and never becomes a second registry: every entry
still resolves through the single canonical ``CapabilityRegistry``.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from athena.capabilities.execute import ExecuteCapability
from athena.capabilities.delegate import DelegateCapability
from athena.capabilities.fs import FilesystemCapability
from athena.capabilities.git import GitCapability
from athena.capabilities.memory import MemoryCapability
from athena.capabilities.skills import SkillsCapability
from athena.policy.credentials import SecretError
from athena.service.core_capability_ports import CoreCapabilityPorts

_logger = logging.getLogger("athena.service")


async def register_core_capabilities(
    owner: Any,
    *,
    registry: Any,
    workspace: Any,
    execution: Any,
    memory: Any,
    skills_store: Any,
    research_store: Any | None = None,
) -> None:
    ports = CoreCapabilityPorts(owner)
    from athena.capabilities.artifacts import ArtifactCapability

    registry.register(FilesystemCapability(workspace))

    registry.register(GitCapability(candidate_resolver=ports.candidate_git_view))
    if ports.artifacts is not None:
        registry.register(ArtifactCapability(ports.artifacts))
    registry.register(
        ExecuteCapability(
            execution,
            workspace,
            artifact_store=ports.artifacts,
            failure_memory=ports.failure_memory,
        )
    )
    from athena.capabilities.diagnostics import DiagnosticsCapability

    if ports.failure_memory is not None:
        registry.register(DiagnosticsCapability(ports.failure_memory))
    registry.register(MemoryCapability(memory))
    registry.register(SkillsCapability(skills_store))
    from athena.capabilities.session_search import SessionSearchCapability

    registry.register(SessionSearchCapability(ports.store_messages))
    registry.register(DelegateCapability(ports.delegation))
    from athena.capabilities.external_delegate import ExternalDelegateCapability
    from athena.delegates.sessions import ExternalDelegateManager

    if ports.delegate_session_store is not None:
        ports.external_delegate_manager = ExternalDelegateManager(
            ports.delegate_registry,
            ports.delegate_session_store,
            dispatcher=ports.dispatcher,
        )
        registry.register(ExternalDelegateCapability(ports.external_delegate_manager))
        ports.register_shutdown_hook(
            "external_delegate_sessions",
            ports.external_delegate_manager.close_all,
        )
    from athena.capabilities.context_blocks import ContextBlocksCapability

    if ports.context_block_store is not None:
        registry.register(ContextBlocksCapability(ports.context_block_store))
    from athena.capabilities.packs import PacksCapability

    if ports.pack_manager is not None:
        registry.register(PacksCapability(ports.pack_manager))
    from athena.capabilities.health import CapabilityHealthCapability

    if ports.capability_health is not None:
        registry.register(CapabilityHealthCapability(ports.capability_health))
    from athena.capabilities.system import MachineCapability, ProcessCapability
    from athena.capabilities.terminal_session import TerminalSessionCapability
    from athena.capabilities.dependency import DependencyCapability
    from athena.capabilities.reflection import CapabilityReflection
    from athena.capabilities.truth import TruthCapability
    from athena.capabilities.research import (
        BraveSearchProvider,
        HttpDiscoveryProvider,
        ResearchCapability,
        TavilySearchProvider,
    )
    from athena.capabilities.scratch import ScratchCapability
    from athena.capabilities.observer import ObserverCapability
    from athena.capabilities.capsule import ProcedureCapsuleCapability
    from athena.capabilities.workflow import WorkflowCapability
    from athena.research.policy import SourcePolicy

    if ports.synthesis is None:
        from athena.synthesis.engine import SynthesisEngine

        ports.synthesis = SynthesisEngine(
            dispatcher=ports.dispatcher,
            research_store=ports.research_store,
        )
    elif ports.dispatcher is not None:
        ports.synthesis.bind_dispatcher(ports.dispatcher)
        ports.synthesis.bind_research_store(ports.research_store)
    fabric = ports.fabric
    if fabric is None:
        raise RuntimeError("capability fabric is not constructed")
    ports.synthesis.bind_proof_sink(fabric.update_generated_proof)
    registry.register(
        ScratchCapability(
            ports.synthesis,
            ports.scratch,
            ports.fabric,
        )
    )
    registry.register(
        ObserverCapability(
            ports.synthesis,
            ports.fabric,
            dispatcher=ports.dispatcher,
        )
    )
    ports.register_shutdown_hook(
        "generated_persistent_sessions",
        ports.synthesis.close_persistent_sessions,
    )

    if TerminalSessionCapability.available():
        ports.terminals = TerminalSessionCapability(
            event_sink=ports.forward_events(ports.require_events()),
        )
        registry.register(ports.terminals)
        ports.optional_capability_health["terminal"] = {
            "installed": True,
            "configured": True,
            "state": "available",
            "reason": None,
        }
    else:
        ports.optional_capability_health["terminal"] = {
            "installed": False,
            "configured": True,
            "state": "unavailable",
            "reason": "pexpect and pyte are required",
        }
        _logger.info("terminal_session capability unavailable: install pexpect and pyte")
    ports.processes = ProcessCapability(execution)
    registry.register(ports.processes)
    registry.register(MachineCapability())
    registry.register(
        CapabilityReflection(
            ports.fabric,
            workflow_store=ports.workflow_store,
            skills_store=skills_store,
            execution_manager=execution,
            device_provider=ports.device_provider,
            policy_engine=ports.policy,
            approval_store=ports.store_approvals,
            health_provider=ports.capability_health,
            runtime_health_provider=ports.runtime_health,
            model_provider=lambda: ports.model_registry,
            mcp_status_provider=ports.mcp_status,
            delegate_provider=lambda: ports.delegate_registry,
        )
    )
    registry.register(TruthCapability(ports.owner))
    registry.register(DependencyCapability(execution))
    if ports.store_input_requests is not None:
        # Descriptor-only registration: the kernel intercepts
        # request_input calls before any dispatcher runs, so the bound
        # executor is a truthful fallback that never runs on a healthy
        # path. The fabric entry makes the affordance discoverable and
        # compilable into the model's tool surface.
        from athena.capabilities.input_request import InputRequestCapability

        registry.register(InputRequestCapability())
    if research_store is not None:
        research_policy = SourcePolicy(
            allowed_domains=tuple(ports.config.research_allowed_domains),
            denied_domains=tuple(ports.config.research_denied_domains),
            allow_private_network=ports.config.research_allow_private_network,
        )
        discovery_endpoints = ports.config.research_discovery_endpoints or (
            (ports.config.research_discovery_endpoint,)
            if ports.config.research_discovery_endpoint
            else ()
        )
        discovery_providers: list[Any] = list(
            HttpDiscoveryProvider(
                endpoint,
                source_policy=research_policy,
                timeout=ports.config.research_discovery_timeout,
            )
            for endpoint in discovery_endpoints
        )

        # Resolve API credentials lazily at search time. This keeps raw
        # keys out of config, capability descriptors, and model context,
        # while allowing the configured SecretManager source (env,
        # keyring, 1Password, Bitwarden, or operator resolver) to rotate.
        def research_credential(name: str):
            def resolve() -> str | None:
                try:
                    return (
                        ports.secrets.resolve(
                            name,
                            owner_task="system",
                            backend="research",
                        )
                        if ports.secrets is not None
                        else None
                    )
                except SecretError:
                    return None

            return resolve

        if ports.config.research_brave_api_key_credential:
            discovery_providers.append(
                BraveSearchProvider(
                    source_policy=research_policy,
                    api_key_resolver=research_credential(
                        ports.config.research_brave_api_key_credential
                    ),
                    timeout=ports.config.research_discovery_timeout,
                )
            )
        if ports.config.research_tavily_api_key_credential:
            discovery_providers.append(
                TavilySearchProvider(
                    source_policy=research_policy,
                    api_key_resolver=research_credential(
                        ports.config.research_tavily_api_key_credential
                    ),
                    timeout=ports.config.research_discovery_timeout,
                )
            )
        registry.register(
            ResearchCapability(
                research_store,
                artifact_store=ports.artifacts,
                source_policy=research_policy,
                discovery_providers=tuple(discovery_providers),
            )
        )
    if ports.workflow_store is not None and ports.fabric is not None:
        workflow_capability = WorkflowCapability(
            ports.workflow_store,
            ports.dispatcher,
            ports.fabric,
            run_store=ports.workflow_run_store,
            external_store=ports.external_effect_store,
            workflow_observer=ports.knowledge.observe_workflow_execution,
        )
        registry.register(workflow_capability)
        registry.register(
            ProcedureCapsuleCapability(
                ports.workflow_store,
                ports.fabric,
                ports.synthesis,
                workflow_capability,
                research_store=ports.research_store,
                dispatcher=ports.dispatcher,
            )
        )
    try:
        from athena.capabilities.debugger import DebuggerCapability

        if DebuggerCapability.available():
            ports.debugger = DebuggerCapability(
                execution_manager=ports.execution,
                event_sink=ports.forward_events(ports.require_events()),
            )
            registry.register(ports.debugger)
            ports.optional_capability_health["debugger"] = {
                "installed": True,
                "configured": True,
                "state": "available",
                "reason": None,
            }
        else:
            ports.optional_capability_health["debugger"] = {
                "installed": False,
                "configured": True,
                "state": "unavailable",
                "reason": "debugpy is not installed",
            }
            _logger.info("debugger capability unavailable: debugpy is not installed")
    except Exception as exc:  # debugpy optional
        ports.optional_capability_health["debugger"] = {
            "installed": False,
            "configured": True,
            "state": "degraded",
            "reason": f"{type(exc).__name__}: {exc}",
        }
        _logger.info("debugger capability unavailable: %s", exc)

    from athena.service.interaction_capabilities import register_interaction_capabilities

    await register_interaction_capabilities(ports, registry)

    # P1/P2 environment families.
    from athena.capabilities.environment import (
        DatabaseCapability,
        NetworkCapability,
        ServiceCapability,
        WorkspaceCapability,
    )
    from athena.capabilities.watch import WatchCapability, WatchRegistry

    registry.register(
        ServiceCapability(
            external_store=ports.external_effect_store,
        )
    )
    registry.register(
        NetworkCapability(
            external_store=ports.external_effect_store,
        )
    )
    ports.database = DatabaseCapability(
        mutation_store=ports.store_mutations,
        artifact_store=ports.artifacts,
        external_store=ports.external_effect_store,
    )
    registry.register(ports.database)
    if getattr(ports, "watch_registry", None) is None:
        ports.watch_registry = WatchRegistry(
            observer_runner=ports.run_watch_observer,
        )
    ports.watches = WatchCapability(
        registry=ports.watch_registry,
        execution_manager=ports.execution,
    )
    registry.register(ports.watches)
    if ports.task_manager is not None:
        ports.task_manager.add_finalize_observer(ports.cleanup_task_watches)
        ports.task_manager.add_finalize_observer(ports.cleanup_task_affordances)
    from athena.causal.checkpoint import CheckpointManager

    # Checkpoints are part of Athena's durable recovery state.  They must
    # survive a service restart and cannot live in the process-global
    # temporary directory, which may be cleaned independently of the
    # transaction ledger.
    checkpoint_root = (
        os.path.join(
            ports.runtime_state_root,
            "checkpoints",
        )
        if ports.runtime_state_root
        else None
    )
    ports.checkpoints = CheckpointManager(root=checkpoint_root or "/tmp/athena-checkpoints")
    if ports.resource_finalizer is not None:
        ports.resource_finalizer.bind_checkpoint_manager(ports.checkpoints)
    ports.reality_gate.bind_checkpoint_manager(ports.checkpoints)
    registry.register(
        WorkspaceCapability(
            checkpoint_manager=ports.checkpoints,
            mutation_store=ports.store_mutations,
            mutation_observer=ports.on_mutation_completed,
            project_index_store=ports.project_index_store,
            project_index_coordinator=ports.project_index_coordinator,
        )
    )
    from athena.capabilities.fusion import FusionCapability

    registry.register(FusionCapability(ports.owner))

    # Capability-owned resource teardown (P1-32).
    if ports.terminals is not None:
        ports.register_shutdown_hook("terminal_sessions", ports.terminals.close_all)
    if ports.debugger is not None:
        ports.register_shutdown_hook("debugger_sessions", ports.debugger.close_all)
    if ports.watch_registry is not None:
        ports.register_shutdown_hook("watch_registry", ports.watch_registry.close)
