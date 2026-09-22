"""Startup composition of policy, dispatch, task, and memory authorities."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from athena.affordances import CapabilityFabric
from athena.artifacts.store import ArtifactStore
from athena.capabilities.dispatcher import CapabilityDispatcher
from athena.capabilities.registry import CapabilityRegistry
from athena.memory.embeddings import FastEmbedProvider
from athena.memory.store import MemoryStore
from athena.policy.engine import PolicyEngine
from athena.protocol.policy import Principal
from athena.service.startup_state import StoreComponents
from athena.skills.lifecycle import SkillLifecycle, SkillStore
from athena.skills.loader import SkillLoader
from athena.tasks.budgets import BudgetTracker
from athena.tasks.cancellation import CancellationManager
from athena.tasks.manager import TaskManager

__all__ = ["AuthorityComponents", "AuthorityCompositionPorts", "AuthorityComposer"]


@dataclass
class AuthorityComponents:
    """Startup phase 4-7 outputs: memory, policy, dispatch, and task authority."""

    memory: MemoryStore
    skill_lifecycle: SkillLifecycle
    skills: SkillStore
    policy: PolicyEngine
    artifacts: ArtifactStore
    registry: CapabilityRegistry
    fabric: CapabilityFabric
    dispatcher: CapabilityDispatcher
    reality_gate: Any
    budgets: BudgetTracker
    task_manager: TaskManager
    cancellations: CancellationManager
    skill_discovery_status: str


class AuthorityCompositionPorts:
    """Explicit resources and application operations for startup composition."""

    _RESOURCE_NAMES = {
        "config": "config",
        "startup_health": "_startup_health",
        "tool_repair_store": "_tool_repair_store",
        "capability_health": "_capability_health",
    }
    _APPLICATION_OPERATIONS = {
        "sync_skills": "_sync_skills",
        "rehydrate_approval_grants": "_rehydrate_approval_grants",
        "forward_events": "_forward_events",
        "mutation_observer": "_on_mutation_completed",
        "shadow_engine": "shadow_engine",
        "require_task_ready": "require_task_ready",
        "mark_execution_uncertain": "_mark_execution_uncertain",
    }

    def __init__(self, owner: Any) -> None:
        self._owner = owner

    def __getattr__(self, name: str) -> Any:
        target = self._RESOURCE_NAMES.get(name) or self._APPLICATION_OPERATIONS.get(name)
        if target is None:
            raise AttributeError(f"authority composition port is not allowed: {name}")
        return getattr(self._owner, target, None)


class AuthorityComposer:
    """Build the existing authorities without owning their runtime state."""

    def __init__(self, *, ports: AuthorityCompositionPorts) -> None:
        self._ports = ports

    async def build(self, store_components: StoreComponents, execution: Any) -> AuthorityComponents:
        cfg = self._ports.config
        db = store_components.db
        events = store_components.events
        tasks = store_components.tasks

        embedding_provider = cfg.memory_embedding_provider or FastEmbedProvider(
            model=cfg.memory_embedding_model,
            cache_dir=cfg.memory_embedding_cache_dir,
        )
        memory = MemoryStore(db, embedding_provider=embedding_provider)
        bundled_skills = Path(__file__).resolve().parents[1] / "bundled_skills"
        skill_loader = SkillLoader(
            search_paths=tuple(cfg.skills_paths),
            bundled_dir=bundled_skills,
        )
        skill_lifecycle = SkillLifecycle(db, events=events)
        skills_store = SkillStore(loader=skill_loader, lifecycle=skill_lifecycle)
        skill_discovery_status = "ok"
        try:
            discovered = await skill_loader.load()
            await self._ports.sync_skills(skill_lifecycle, discovered)
            self._ports.startup_health["checks"]["skills"] = {
                "status": "ok",
                "blocking": False,
                "discovered": len(discovered),
            }
        except Exception as exc:
            skill_discovery_status = f"failed: {exc}"
            self._ports.startup_health["checks"]["skills"] = {
                "status": "degraded",
                "blocking": False,
                "error": str(exc),
            }

        policy = PolicyEngine(profile=cfg.autonomy_level)
        await self._ports.rehydrate_approval_grants(
            store_components.approvals, store_components.continuations
        )
        artifacts = ArtifactStore(root=cfg.artifact_root)
        registry = CapabilityRegistry()
        fabric = CapabilityFabric(registry, store=store_components.generated_store)
        dispatcher = CapabilityDispatcher(
            registry,
            policy,
            principal=Principal("agent", cfg.cache_namespace),
            mutation_store=store_components.mutations,
            approval_store=store_components.approvals,
            continuation_store=store_components.continuations,
            repair_store=self._ports.tool_repair_store,
            event_sink=self._ports.forward_events(events),
            artifact_store=artifacts,
            mutation_observer=self._ports.mutation_observer,
            fabric=fabric,
            health=self._ports.capability_health,
            failure_memory=store_components.failure_memory,
        )
        from athena.reality import RealityGate

        reality_gate = RealityGate(self._ports.shadow_engine())
        dispatcher.set_reality_gate(reality_gate)
        budgets = BudgetTracker(task_store=tasks)
        dispatcher.set_budget_tracker(budgets)
        artifacts.set_budget_tracker(budgets)
        task_manager = TaskManager(
            task_store=tasks,
            events=events,
            sessions=store_components.sessions,
            budgets=budgets,
            admission=self._ports.require_task_ready,
            principal_id=cfg.cache_namespace,
            finalizations=store_components.pending_finalization_store,
            steering_store=store_components.steering_store,
        )
        task_manager.set_model_response_store(store_components.model_response_store)
        execution.set_recovery_sink(self._ports.mark_execution_uncertain)
        cancellations = CancellationManager(
            task_manager=task_manager,
            execution_manager=execution,
            task_store=tasks,
        )
        task_manager._cancellations = cancellations  # noqa: SLF001
        return AuthorityComponents(
            memory=memory,
            skill_lifecycle=skill_lifecycle,
            skills=skills_store,
            policy=policy,
            artifacts=artifacts,
            registry=registry,
            fabric=fabric,
            dispatcher=dispatcher,
            reality_gate=reality_gate,
            budgets=budgets,
            task_manager=task_manager,
            cancellations=cancellations,
            skill_discovery_status=skill_discovery_status,
        )
