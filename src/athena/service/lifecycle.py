"""Service lifecycle mechanism for the façade (P1-10).

``start``/``_start_impl``/``stop`` moved verbatim from
``athena.service.service``. This is a subordinate mechanism, not a second
authority: the lifecycle wires and tears down resources that all remain
owned by the :class:`AthenaService` façade instance (``self.ports``) —
stores, kernel, worker, scheduler, dispatcher, compiler, recovery — and
startup stays a transaction over resources acquired in dependency order.
"""

from __future__ import annotations

from dataclasses import dataclass

from athena.affordances import CapabilityFabric
from athena.capabilities.registry import CapabilityRegistry
from athena.capabilities.schedule import ScheduleAPI
from athena.capabilities.schedule import ScheduleCapability
from athena.execution.manager import ExecutionManager
from athena.knowledge.pipeline import KnowledgePipeline
from athena.protocol.tasks import WorkspaceSpec
from athena.scheduler.scheduler import Scheduler
from athena.service.authority_composition import (
    AuthorityComponents,
    AuthorityComposer,
    AuthorityCompositionPorts,
)
from athena.service.lifecycle_ports import LifecyclePorts
from athena.service.startup_recovery import StartupRecovery, StartupRecoveryPorts
from athena.service.startup_state import StoreComponents
from athena.skills.lifecycle import SkillLifecycle
from athena.state.events import EventStore
from athena.state.events import FAST_EVENT_TYPES
from athena.tasks.manager import TaskManager
from typing import Any
import asyncio
import logging
import json


_logger = logging.getLogger("athena.service")


@dataclass
class FinalizerComponents:
    """Startup phase 7.5 outputs: observers bounded after task finalization."""

    knowledge: KnowledgePipeline
    delivery: Any


class ServiceLifecycle:
    """Owns the start/stop transaction of the service façade."""

    def __init__(self, service: Any, *, ports: LifecyclePorts | None = None) -> None:
        self.ports = ports or LifecyclePorts(service)
        self._authority_composer = AuthorityComposer(ports=AuthorityCompositionPorts(service))

    async def start(self) -> None:
        if self.ports.started:
            return

        try:
            await self.ports.start_impl()
        except BaseException:
            self.ports.startup_health = {
                **self.ports.startup_health,
                "status": "failed",
                "blocking_failures": ["service_startup"],
            }
            # Startup is a transaction over resources acquired in order. The
            # service must not leak a DB, worker, poller, scheduler, client, or
            # runtime when a later stage fails.
            try:
                await asyncio.shield(self.ports.stop())
            except BaseException as cleanup_error:
                _logger.error("startup unwind failed: %s", cleanup_error, exc_info=True)
            raise

    async def _build_authorities(
        self,
        *,
        store_components: StoreComponents,
        execution: ExecutionManager,
    ) -> AuthorityComponents:
        """Startup phases 4-7: compose authorities on explicit store inputs."""
        authorities = await self._authority_composer.build(store_components, execution)
        for attr, value in {
            "_memory": authorities.memory,
            "_skills": authorities.skills,
            "_skill_lifecycle": authorities.skill_lifecycle,
            "_skill_discovery_status": authorities.skill_discovery_status,
            "_policy": authorities.policy,
            "_artifacts": authorities.artifacts,
            "_registry": authorities.registry,
            "_fabric": authorities.fabric,
            "_dispatcher": authorities.dispatcher,
            "_reality_gate": authorities.reality_gate,
            "_budgets": authorities.budgets,
            "_task_manager": authorities.task_manager,
            "_cancellations": authorities.cancellations,
        }.items():
            setattr(self.ports, attr.removeprefix("_"), value)
        return authorities

    async def _build_finalize_observers(
        self,
        *,
        store_components: StoreComponents,
    ) -> FinalizerComponents:
        """Startup phase 7.5: post-finalization knowledge and delivery."""
        messages = store_components.messages
        events = store_components.events
        memory = self.ports.memory
        skill_lifecycle = self.ports.skill_lifecycle

        self.ports.knowledge = KnowledgePipeline(
            messages=messages,
            memory_store=memory,
            skill_lifecycle=skill_lifecycle,
            workflow_store=self.ports.workflow_store,
            events=events,
            principal_id=self.ports.config.cache_namespace,
        )

        from athena.delivery import DeliveryManager

        self.ports.delivery = DeliveryManager(
            event_store=events,
            external_store=self.ports.external_effect_store,
        )
        return FinalizerComponents(knowledge=self.ports.knowledge, delivery=self.ports.delivery)

    async def _validate_startup_readiness(self) -> None:
        """Check deployment capability and durable intake contracts."""
        capability_profile = await self.ports.validate_required_capabilities()
        self.ports.startup_health["checks"]["capability_profile"] = capability_profile
        if capability_profile.get("status") != "ok":
            missing = ", ".join(
                f"{item['id']}: {item['reason']}" for item in capability_profile.get("missing", ())
            )
            raise RuntimeError(f"required capability profile is not ready: {missing}")
        intake_recovery = await self.ports.reconcile_created_intake()
        self.ports.startup_health["checks"]["task_intake"] = {
            "status": "degraded" if intake_recovery["quarantined"] else "ok",
            "blocking": bool(intake_recovery["quarantined"]),
            **intake_recovery,
        }

    async def _validate_generated_record(self, generated: Any) -> bool:
        """Retain current records; persist stale state and reject stale proof."""
        status = await self.ports.synthesis.evidence_status(
            generated,
            self.ports.research_store,
        )
        if status["status"] == "CURRENT":
            return True
        owner = (
            generated.project_scope if generated.scope.value == "project" else generated.user_scope
        ) or str(generated.provenance.get("owner") or "")
        if owner and self.ports.generated_store is not None:
            try:
                await self.ports.generated_store.transition(
                    generated.id,
                    "STALE",
                    owner=owner,
                    reason=json.dumps(status, sort_keys=True),
                )
            except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
                _logger.warning(
                    "could not persist stale generated capability %s: %s",
                    generated.id,
                    exc,
                )
        return False

    async def _register_generated_capabilities(
        self,
        *,
        registry: CapabilityRegistry,
        fabric: CapabilityFabric,
        workspace: WorkspaceSpec,
        events: EventStore,
    ) -> None:
        """Restore generated machinery and rebuild proof metrics from events."""
        cfg = self.ports.config
        from athena.capabilities.synthesis import SynthesisCapability

        registry.register(
            SynthesisCapability(
                self.ports.synthesis,
                fabric,
                research_store=self.ports.research_store,
                scratch=self.ports.scratch,
            )
        )

        await fabric.load_persisted(
            lambda generated: self.ports.synthesis.restore_executor(
                generated,
                proof_sink=fabric.update_generated_proof,
                workspace_root=workspace.root,
            ),
            project_id=workspace.id,
            user_id=cfg.cache_namespace,
            record_validator=lambda generated: self._validate_generated_record(generated),
        )
        await self.ports.synthesis.replay_event_metrics(events)
        self.ports.synthesis_event_observer = self.ports.synthesis.observe_event
        events.subscribe(
            self.ports.synthesis_event_observer,
            event_types={"CapabilityCompleted", "VerificationCompleted"},
        )

    async def _register_schedule_capabilities(
        self,
        *,
        registry: CapabilityRegistry,
        workspace: WorkspaceSpec,
        task_manager: TaskManager,
    ) -> None:
        """Build scheduler/maintenance owners, then register their capabilities."""
        cfg = self.ports.config
        events = self.ports.store_events
        scheduler = Scheduler(
            store=self.ports.store_schedules,
            task_manager=task_manager,
            admission=self.ports.require_task_ready,
            intake=self.ports.submit_spec,
            max_concurrent=cfg.scheduler_max_concurrent,
            loop_interval_seconds=cfg.scheduler_interval_seconds,
        )
        self.ports.scheduler = scheduler
        events.subscribe(scheduler.notify_event, exclude_event_types=FAST_EVENT_TYPES)
        schedule_api = ScheduleAPI(scheduler, task_manager)
        self.ports.schedule_api = schedule_api
        registry.register(ScheduleCapability(schedule_api))
        from athena.capabilities.watch import WatchRegistry

        if self.ports.watch_registry is None:
            self.ports.watch_registry = WatchRegistry(
                observer_runner=self.ports.run_watch_observer,
            )
        from athena.capabilities.maintain import MaintenanceCapability

        maintenance = MaintenanceCapability(
            schedule_api,
            watch_registry=self.ports.watch_registry,
            workspace=workspace,
            execution_manager=self.ports.execution,
            fabric=self.ports.fabric,
            principal_id=cfg.cache_namespace,
        )
        registry.register(maintenance)
        try:
            restored = await maintenance.rehydrate()
            if restored:
                _logger.info("rehydrated %d maintenance observers", restored)
        except Exception as exc:
            self.ports.watch_registry.record_rehydration_failure(
                exc, contract_id="maintenance_contracts"
            )
            _logger.warning("maintenance observer rehydration failed: %s", exc)

    def _bind_pack_lifecycle(
        self,
        *,
        skill_lifecycle: SkillLifecycle,
        workspace: WorkspaceSpec,
        events: EventStore,
        task_manager: TaskManager,
    ) -> None:
        """Bind pack integrations to the live canonical service authorities."""
        self.ports.pack_manager.lifecycle.bind_integrations(
            skill_lifecycle=skill_lifecycle,
            workflow_store=self.ports.workflow_store,
            fabric=self.ports.fabric,
            dispatcher=self.ports.dispatcher,
            mcp_adapter=self.ports.mcp,
            mcp_client_sink=self.ports.mcp_clients.append,
            event_store=events,
            hook_outbox=self.ports.pack_hook_outbox,
            task_intake=self.ports.submit,
            task_lookup=task_manager.get,
            workspace=workspace,
        )

    async def _rehydrate_enabled_packs(self, *, task_manager: TaskManager) -> None:
        """Rehydrate packs, replay hooks, and quarantine dependent tasks."""
        lifecycle = self.ports.pack_manager.lifecycle
        try:
            activated = await lifecycle.rehydrate_enabled()
            await lifecycle.replay_hook_outbox()
            await lifecycle.start_hook_dispatcher()
            failures = lifecycle.rehydration_failures()
            unavailable = {str(item["pack_id"]) for item in failures}
            quarantined = await self.ports.quarantine_tasks_for_packs(
                task_store=self.ports.store_tasks,
                task_manager=task_manager,
                unavailable=unavailable,
            )
            self.ports.startup_health["checks"]["enabled_packs"] = {
                "status": "degraded" if failures else "ok",
                "blocking": False,
                "activated": activated,
                "failures": failures,
                "quarantined_tasks": quarantined,
            }
        except Exception as exc:
            _logger.warning("enabled capability-pack rehydration failed: %s", exc)
            self.ports.startup_health["checks"]["enabled_packs"] = {
                "status": "degraded",
                "blocking": False,
                "error": str(exc),
            }

    async def _run_startup_recovery(self, *, coordinator: Any, finalizer: Any) -> None:
        """Reconcile durable resource, task, provider, and external sagas."""
        result = await StartupRecovery(
            StartupRecoveryPorts(
                events=self.ports.store_events,
                tasks=self.ports.store_tasks,
                mutations=self.ports.store_mutations,
                execution_store=self.ports.store_executions,
                runtime_sessions=self.ports.store_runtime_sessions,
                execution=self.ports.execution,
                task_manager=self.ports.task_manager,
                pending_finalization_store=self.ports.pending_finalization_store,
            )
        ).reconcile(coordinator=coordinator, finalizer=finalizer)
        self.ports.startup_health["checks"]["resource_obligations"] = {
            "status": "ok" if result.resource_health["unresolved_count"] == 0 else "degraded",
            "blocking": result.resource_health["unresolved_count"] > 0,
            "unresolved_count": result.resource_health["unresolved_count"],
        }
        self.ports.startup_health["checks"]["pending_finalizations"] = {
            "status": "ok" if result.pending_remaining == 0 else "degraded",
            "blocking": result.pending_remaining > 0,
            "recovered": result.pending_recovered,
            "remaining": result.pending_remaining,
        }
        if result.completion_recovered:
            _logger.info("recovered proven reality completions: %d", result.completion_recovered)
        self.ports.recovery_status = result.recovery_status
        self.ports.recovery_summary = result.recovery_summary
        self.ports.recovery_error = result.recovery_error
        if any(result.recovery_summary.values()):
            _logger.info("crash recovery reconciled: %s", result.recovery_summary)

    async def _reconcile_provider_sagas(self) -> None:
        """Replay task-side provider outcomes before worker startup."""
        provider_recovery = await self.ports.reconcile_provider_outcomes()
        unresolved_provider = await self.ports.model_response_store.list_unresolved_attempts()
        self.ports.provider_recovery_health = {
            "state": "degraded" if unresolved_provider else "ready",
            "unresolved_count": len(unresolved_provider),
            "replayed": provider_recovery["replayed"],
            "error": None,
        }
        self.ports.startup_health["checks"]["provider_outcomes"] = {
            "status": "degraded" if unresolved_provider else "ok",
            "blocking": bool(unresolved_provider),
            "unresolved_count": len(unresolved_provider),
            "replayed": provider_recovery["replayed"],
        }

    async def _reconcile_external_sagas(self) -> None:
        """Fail closed if transaction, fusion, or external receipts are uncertain."""
        events = self.ports.store_events

        transaction_recovered = await self.ports.reality_gate.reconcile_startup()
        if transaction_recovered:
            _logger.warning("transactional work requires reconciliation: %d", transaction_recovered)
        shadow_recovered = await self.ports.shadow_engine().reconcile_startup(events)
        if shadow_recovered:
            _logger.warning("fusion branches require reconciliation: %d", shadow_recovered)
        external_recovered = await self.ports.external_effect_store.reconcile_startup()
        if external_recovered:
            _logger.warning(
                "external effects require operator reconciliation: %d",
                len(external_recovered),
            )
            for receipt in external_recovered:
                evidence = dict((receipt.get("response") or {}).get("recovery") or {})
                await events.append_event(
                    "ExternalEffectRecoveryRequired", evidence, task_id=receipt.get("task_id")
                )

    async def _start_impl(self) -> None:
        """Acquire service resources in dependency order; ``start`` unwinds."""
        from athena.service.startup import StartupPhaseRunner

        await StartupPhaseRunner(self).run()

    async def stop(self) -> None:
        """Run the canonical stop transaction through extracted phases."""
        from athena.service.stop_sequence import stop

        await stop(self.ports)
