"""Service lifecycle mechanism for the façade (P1-10).

``start``/``_start_impl``/``stop`` moved verbatim from
``athena.service.service``. This is a subordinate mechanism, not a second
authority: the lifecycle wires and tears down resources that all remain
owned by the :class:`AthenaService` façade instance (``self._svc``) —
stores, kernel, worker, scheduler, dispatcher, compiler, recovery — and
startup stays a transaction over resources acquired in dependency order.
"""

from __future__ import annotations

from pathlib import Path

from athena.affordances import CapabilityFabric
from athena.affordances import GeneratedCapabilityStore
from athena.artifacts.store import ArtifactStore
from athena.capabilities.dispatcher import CapabilityDispatcher
from athena.capabilities.registry import CapabilityRegistry
from athena.capabilities.schedule import ScheduleAPI
from athena.capabilities.schedule import ScheduleCapability
from athena.context.compiler import ContextCompiler
from athena.context.digest import ContextDigestStore
from athena.execution.container import ContainerBackend
from athena.execution.manager import ExecutionManager
from athena.execution.runtimes import PythonRuntime
from athena.execution.runtimes import ShellRuntime
from athena.execution.runtimes.node import NodeRuntime
from athena.execution.runtimes.powershell import PowerShellRuntime
from athena.kernel.continuations import ContinuationStore
from athena.kernel.kernel import AgentKernel
from athena.kernel.termination import TerminationEvaluator
from athena.knowledge.pipeline import KnowledgePipeline
from athena.mcp.adapter import MCPAdapter
from athena.memory.store import MemoryStore
from athena.memory.embeddings import FastEmbedProvider
from athena.models.registry import ProviderRegistry
from athena.packs.store import PackStore
from athena.policy.credentials import SecretManager
from athena.policy.engine import PolicyEngine
from athena.project.index.builder import ProjectIndexBuilder
from athena.project.index.coordinator import ProjectIndexCoordinator
from athena.project.index.store import ProjectIndexStore
from athena.protocol.tasks import TaskStatus
from athena.protocol.tasks import WorkspaceSpec
from athena.protocol.policy import Principal
from athena.scheduler.scheduler import Scheduler
from athena.service.config import DEFAULT_DB_PATH
from athena.skills.lifecycle import SkillLifecycle
from athena.skills.lifecycle import SkillStore
from athena.skills.loader import SkillLoader
from athena.state.approvals import ApprovalStore
from athena.state.context_blocks import ContextBlockStore
from athena.state.database import Database
from athena.state.delegate_sessions import DelegateSessionStore
from athena.state.events import EventStore
from athena.state.events import FAST_EVENT_TYPES
from athena.state.executions import ExecutionStore
from athena.state.external_effects import ExternalEffectStore
from athena.state.failure_memory import FailureMemory
from athena.state.messages import MessageStore
from athena.state.mutations import MutationStore
from athena.state.runtime_sessions import RuntimeSessionStore
from athena.state.schedules import ScheduleStore
from athena.state.self_host import SelfHostMissionStore
from athena.state.sessions import SessionRepository
from athena.state.tasks import TaskStore
from athena.state.tool_repairs import ToolRepairStore
from athena.tasks.budgets import BudgetTracker
from athena.tasks.cancellation import CancellationManager
from athena.tasks.delegation import DelegationManager
from athena.tasks.manager import TaskManager
from athena.tasks.worker import TaskWorker
from athena.tasks.worker import WorkerConfig
from typing import Any
import asyncio
import logging
import json
import os
import tempfile


_logger = logging.getLogger("athena.service")


class ServiceLifecycle:
    """Owns the start/stop transaction of the service façade."""

    def __init__(self, service: Any) -> None:
        self._svc = service

    async def start(self) -> None:
        if self._svc._started:
            return

        try:
            await self._svc._start_impl()
        except BaseException:
            self._svc._startup_health = {
                **self._svc._startup_health,
                "status": "failed",
                "blocking_failures": ["service_startup"],
            }
            # Startup is a transaction over resources acquired in order. The
            # service must not leak a DB, worker, poller, scheduler, client, or
            # runtime when a later stage fails.
            try:
                await asyncio.shield(self._svc.stop())
            except BaseException as cleanup_error:
                _logger.error("startup unwind failed: %s", cleanup_error, exc_info=True)
            raise

    async def _start_impl(self) -> None:
        """Acquire service resources in dependency order.

        ``start`` owns the unwind boundary; keeping acquisition in this helper
        makes it impossible for a new stage to accidentally bypass cleanup.
        """

        cfg = self._svc.config
        self._svc._startup_health = {
            "status": "starting",
            "checks": {},
            "blocking_failures": [],
        }
        self._svc._recovery_status = "starting"
        self._svc._recovery_summary = {}
        self._svc._recovery_error = None
        workspace = WorkspaceSpec(
            id="root",
            root=cfg.workspace_root or self._svc._default_workspace.root,
        )
        self._svc._default_workspace = workspace

        # 1. State: DB + stores (migrations apply lazily on first query).
        db_path = cfg.db_path or DEFAULT_DB_PATH()
        db = Database(db_path)
        await db._ensure_ready()  # noqa: SLF001 - apply migrations exactly once, deterministically
        self._svc._db = db
        self._svc._runtime_state_root = (
            tempfile.mkdtemp(prefix="athena-runtime-")
            if db_path == ":memory:"
            else os.path.join(os.path.dirname(os.path.abspath(db_path)), "fusion")
        )
        sessions = SessionRepository(db)
        tasks = TaskStore(db)
        events = EventStore(db)
        messages = MessageStore(db)
        approvals = ApprovalStore(db)
        mutations = MutationStore(db)
        schedules = ScheduleStore(db)
        continuations = ContinuationStore(db)
        from athena.state.input_requests import InputRequestStore

        input_requests = InputRequestStore(db)
        self._svc._sessions = sessions
        self._svc._store_tasks = tasks
        self._svc._store_events = events
        self._svc._store_messages = messages
        self._svc._store_approvals = approvals
        self._svc._store_mutations = mutations
        self._svc._external_effect_store = ExternalEffectStore(db)
        self._svc._store_schedules = schedules
        self._svc._store_continuations = continuations
        self._svc._store_input_requests = input_requests
        from athena.worldstate import WorldStateStore

        self._svc._world_state_store = WorldStateStore(db)
        from athena.workflows import WorkflowStore

        self._svc._workflow_store = WorkflowStore(db)
        from athena.workflows import WorkflowRunStore

        self._svc._workflow_run_store = WorkflowRunStore(db)
        self._svc._generated_store = GeneratedCapabilityStore(db)
        from athena.research import ResearchStore

        self._svc._research_store = ResearchStore(db)
        from athena.state.provider_usage import ProviderUsageStore

        self._svc._provider_usage_store = ProviderUsageStore(db)
        self._svc._project_index_store = ProjectIndexStore(db)
        self._svc._project_index_builder = ProjectIndexBuilder()
        self._svc._project_index_coordinator = ProjectIndexCoordinator(
            self._svc._project_index_store,
            self._svc._project_index_builder,
        )
        self._svc._failure_memory = FailureMemory(db)

        # 2. Credentials (SecretManager owns resolution + leases).
        self._svc._secrets = SecretManager()
        self._svc._configure_hermes_referee()
        await self._svc._preflight_hermes_referee()

        # 3. Execution + runtimes.
        runtime_sessions = RuntimeSessionStore(db)
        execution_store = ExecutionStore(db)
        execution = ExecutionManager(
            runtime_session_store=runtime_sessions,
            execution_store=execution_store,
            event_sink=self._svc._forward_events(events),
        )
        # Keep container execution optional, but register the real backend so
        # a workspace selecting ``execution_backend="container"`` reaches the
        # same canonical execution authority as local execution.
        execution.register_backend(ContainerBackend())
        execution.register_runtime(PythonRuntime())
        execution.register_runtime(ShellRuntime())
        if PowerShellRuntime.available():
            execution.register_runtime(PowerShellRuntime())
        if NodeRuntime.available():
            execution.register_runtime(NodeRuntime())
        self._svc._execution = execution
        self._svc._store_runtime_sessions = runtime_sessions
        self._svc._store_executions = execution_store
        self._svc._self_host_missions = SelfHostMissionStore(db)
        self._svc._tool_repair_store = ToolRepairStore(db)
        self._svc._context_block_store = ContextBlockStore(db)
        self._svc._pack_store = PackStore(db)
        from athena.packs.manager import PackManager

        self._svc._pack_manager = PackManager(
            self._svc._pack_store,
            install_root=os.path.join(self._svc._runtime_state_root, "packs"),
        )
        self._svc._delegate_session_store = DelegateSessionStore(db)
        from athena.state.capability_health import CapabilityHealthStore

        self._svc._capability_health_store = CapabilityHealthStore(db)
        from athena.capabilities.health import CapabilityHealth

        self._svc._capability_health = CapabilityHealth(store=self._svc._capability_health_store)
        try:
            await self._svc._capability_health.load(await self._svc._capability_health_store.list())
            self._svc._startup_health["checks"]["capability_health"] = {
                "status": "ok",
                "blocking": False,
            }
        except Exception as exc:
            _logger.warning("capability health rehydration failed: %s", exc)
            self._svc._startup_health["checks"]["capability_health"] = {
                "status": "degraded",
                "blocking": False,
                "error": str(exc),
            }

        # 4. Memory + skills.
        embedding_provider = cfg.memory_embedding_provider or FastEmbedProvider(
            model=cfg.memory_embedding_model,
            cache_dir=cfg.memory_embedding_cache_dir,
        )
        memory = MemoryStore(db, embedding_provider=embedding_provider)
        self._svc._memory = memory
        # Bundled skills are a small, versioned first-party library. Explicit
        # project/user paths remain higher precedence and can shadow a bundled
        # name+version, while an empty config still gives a useful out-of-box
        # release/debugging workflow.
        bundled_skills = Path(__file__).resolve().parents[1] / "bundled_skills"
        skill_loader = SkillLoader(
            search_paths=tuple(cfg.skills_paths),
            bundled_dir=bundled_skills,
        )
        skill_lifecycle = SkillLifecycle(db, events=events)
        skills_store = SkillStore(loader=skill_loader, lifecycle=skill_lifecycle)
        self._svc._skills = skills_store
        self._svc._skill_lifecycle = skill_lifecycle
        try:
            discovered = await skill_loader.load()
            await self._svc._sync_skills(skill_lifecycle, discovered)
            self._svc._skill_discovery_status = "ok"
            self._svc._startup_health["checks"]["skills"] = {
                "status": "ok",
                "blocking": False,
                "discovered": len(discovered),
            }
        except Exception as exc:
            _logger.warning("skill discovery failed: %s", exc)
            # Track skill discovery status for visibility
            self._svc._skill_discovery_status = f"failed: {exc}"
            self._svc._startup_health["checks"]["skills"] = {
                "status": "degraded",
                "blocking": False,
                "error": str(exc),
            }

        # 4. Policy engine.
        policy = PolicyEngine(profile=cfg.autonomy_level)
        self._svc._policy = policy
        await self._svc._rehydrate_approval_grants(approvals, continuations)

        # 5. Artifacts (construct BEFORE dispatcher so it can be injected).
        self._svc._artifacts = ArtifactStore(root=cfg.artifact_root)

        # 6. Capability registry + dispatcher (single path, INV-004).
        registry = CapabilityRegistry()
        self._svc._registry = registry
        fabric = CapabilityFabric(registry, store=self._svc._generated_store)
        self._svc._fabric = fabric
        dispatcher = CapabilityDispatcher(
            registry,
            policy,
            principal=Principal("agent", cfg.cache_namespace),
            mutation_store=mutations,
            approval_store=approvals,
            continuation_store=continuations,
            repair_store=self._svc._tool_repair_store,
            event_sink=self._svc._forward_events(events),
            artifact_store=self._svc._artifacts,
            mutation_observer=self._svc._on_mutation_completed,
            fabric=fabric,
            health=self._svc._capability_health,
            failure_memory=self._svc._failure_memory,
        )
        self._svc._dispatcher = dispatcher
        from athena.reality import RealityGate

        self._svc._reality_gate = RealityGate(self._svc.shadow_engine())
        dispatcher.set_reality_gate(self._svc._reality_gate)

        # 7. TaskManager (needs budgets/cancellations, built a bit later).
        budgets = BudgetTracker(task_store=tasks)
        self._svc._budgets = budgets
        dispatcher.set_budget_tracker(budgets)
        self._svc._artifacts.set_budget_tracker(budgets)
        task_manager = TaskManager(
            task_store=tasks,
            events=events,
            sessions=sessions,
            budgets=budgets,
            admission=self._svc.require_task_ready,
            principal_id=cfg.cache_namespace,
        )
        self._svc._task_manager = task_manager

        cancellations = CancellationManager(
            task_manager=task_manager,
            execution_manager=execution,
            task_store=tasks,
        )
        self._svc._cancellations = cancellations
        task_manager._cancellations = cancellations  # noqa: SLF001

        # Post-finalization knowledge pipeline (BUILDSPEC 64/68): eligible
        # completed/partial tasks may feed memory + skill candidates. Bound
        # after all stores exist; the observer itself is failure-isolated.
        self._svc._knowledge = KnowledgePipeline(
            messages=messages,
            memory_store=memory,
            skill_lifecycle=skill_lifecycle,
            workflow_store=self._svc._workflow_store,
            events=events,
            principal_id=cfg.cache_namespace,
        )
        task_manager.add_finalize_observer(self._svc._knowledge)

        # Terminal-result delivery (P1-19): TaskSpec.delivery finally has a
        # consumer. Bound as a finalize observer — delivery runs after the
        # result is durable and its failures never destabilize finalization.
        from athena.delivery import DeliveryManager

        self._svc._delivery = DeliveryManager(
            event_store=events,
            external_store=self._svc._external_effect_store,
        )
        task_manager.add_finalize_observer(self._svc._delivery)

        # 8. Models + router (with role-divided policies: "summarizer",
        # "judge", etc. can be pinned to specific models in config; roles
        # without an entry fall back to the user's primary/global choice).
        model_registry = ProviderRegistry()
        self._svc._register_providers(model_registry)
        self._svc._model_registry = model_registry
        provider_readiness = model_registry.readiness()
        if provider_readiness.get("state") == "ready":
            self._svc._startup_health["checks"]["model_provider"] = {
                "status": "ok",
                "blocking": False,
                "providers": list(model_registry.names()),
                "readiness": provider_readiness,
            }
        else:
            # A production service must never pretend that a built-in fake
            # model is configured. Starting without a provider is useful for
            # setup/inspection, but every interface must expose the explicit
            # first-run state and model requests must fail clearly.
            model_provider_check: dict[str, Any] = {
                "status": "unconfigured"
                if provider_readiness.get("state") == "unconfigured"
                else "degraded",
                "blocking": False,
                "providers": list(model_registry.names()),
                "reason": (
                    "configure a model provider before submitting agent work"
                    if provider_readiness.get("state") == "unconfigured"
                    else "no configured model provider is ready for submission"
                ),
            }
            if provider_readiness.get("state") != "unconfigured":
                model_provider_check["readiness"] = provider_readiness
            self._svc._startup_health["checks"]["model_provider"] = model_provider_check
        router = self._svc._build_model_router(model_registry, cfg)
        self._svc._router = router

        # 9. Context compiler (with a model-backed compression summarizer so older
        # transcript is genuinely summarized, not just truncated).
        compiler = ContextCompiler(
            message_store=messages,
            memory_store=memory,
            skill_loader=skills_store,
            capability_registry=fabric,
            artifact_store=self._svc._artifacts,
            research_store=self._svc._research_store,
            context_block_store=self._svc._context_block_store,
            context_digest_store=ContextDigestStore(db),
            summarizer=self._svc._make_model_summarizer(model_registry),
            context_window=cfg.context_window,
            reserve_output=cfg.reserve_output,
            principal_id=cfg.cache_namespace,
            workspace_reader=self._svc._workspace_reader(),
        )
        self._svc._compiler = compiler

        # 10. Kernel.
        verifier = self._svc._build_verifier(
            execution=execution,
            dispatcher=self._svc._dispatcher,
            artifact_store=self._svc._artifacts,
            capability_registry=fabric,
            model_registry=router,  # ModelRouter: judge role routing
            evidence_provider=self._svc._verification_evidence,
            inference_broker=self._svc._make_judge_broker(),
        )
        self._svc._acceptance_verifier = verifier
        from athena.reality import RealityCoordinator, ShadowCandidateVerifier

        coordinator = RealityCoordinator(
            shadow_engine=self._svc.shadow_engine(),
            reality_gate=self._svc._reality_gate,
            candidate_verifier=ShadowCandidateVerifier(verifier),
            event_sink=self._svc._forward_events(events),
            default_criteria_source=self._svc._project_profile_for_completion,
            project_index_provider=self._svc._project_index_for_completion,
        )
        self._svc._reality_coordinator = coordinator
        kernel = AgentKernel(
            task_store=tasks,
            events=events,
            task_manager=task_manager,
            messages=messages,
            registry=model_registry,
            router=router,
            budgets=budgets,
            context_compiler=compiler,
            termination=TerminationEvaluator(
                acceptance_verifier=verifier,
                defer_reality_verification=lambda task: (
                    self._svc._reality_gate.active_branch(task.id) is not None
                    or self._svc._reality_gate.checkpoint_id(task.id) is not None
                ),
            ),
            dispatch_factory=self._svc._dispatch_factory,
            continuation_store=continuations,
            workflow_run_store=self._svc._workflow_run_store,
            input_request_store=input_requests,
            parked_slot_wait_s=cfg.parked_slot_wait_s,
            provider_usage_store=self._svc._provider_usage_store,
            interpreter=self._svc._make_interpreter(),
            reality_coordinator=coordinator,
            secret_manager=self._svc._secrets,
        )
        self._svc._kernel = kernel

        # 10.9 Body-observation bridge (P1-15): terminal sessions announce
        # large screen renders as RuntimeScreenChanged events; the bridge
        # converts them into typed TerminalScreenChanged observations and
        # offers them to the kernel's interpreter path. The kernel enforces
        # the same triggering/budget rules as loop-side offers.
        def _on_runtime_screen_changed(event) -> None:
            payload = getattr(event, "payload", None) or {}
            observation = _body_observation_from_screen_event(event, payload)
            if observation is not None:
                asyncio.ensure_future(kernel.offer_body_observation(observation))

        events.subscribe(
            _on_runtime_screen_changed,
            event_types={"RuntimeScreenChanged"},
        )

        # 11. Delegation (needs kernel).
        delegation = DelegationManager(
            task_manager=task_manager,
            kernel=kernel,
            budgets=budgets,
            cancellations=cancellations,
            execution_manager=execution,
        )
        self._svc._delegation = delegation

        # 12. Register core capabilities (bind executors to current handles).
        await self._svc._register_core_capabilities(
            registry=registry,
            workspace=workspace,
            execution=execution,
            memory=memory,
            skills_store=skills_store,
            research_store=self._svc._research_store,
        )

        # Rehydrate only validated project/user machinery. Task-local
        # capabilities are intentionally recreated by the owning task and
        # never survive terminal cleanup or a process restart.
        from athena.capabilities.synthesis import SynthesisCapability

        registry.register(
            SynthesisCapability(
                self._svc._synthesis,
                fabric,
                research_store=self._svc._research_store,
                scratch=self._svc._scratch,
            )
        )

        async def _current_generated_evidence(generated):
            status = await self._svc._synthesis.evidence_status(
                generated,
                self._svc._research_store,
            )
            if status["status"] == "CURRENT":
                return True
            owner = (
                generated.project_scope
                if generated.scope.value == "project"
                else generated.user_scope
            ) or str(generated.provenance.get("owner") or "")
            if owner and self._svc._generated_store is not None:
                try:
                    await self._svc._generated_store.transition(
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

        await fabric.load_persisted(
            lambda generated: self._svc._synthesis.restore_executor(
                generated,
                proof_sink=fabric.update_generated_proof,
                workspace_root=workspace.root,
            ),
            project_id=workspace.id,
            user_id=cfg.cache_namespace,
            record_validator=_current_generated_evidence,
        )
        # Generated proof metrics are rebuilt from canonical events and then
        # kept current by the same append-only event stream.  This makes a
        # verification count evidence, not caller-supplied promotion metadata.
        await self._svc._synthesis.replay_event_metrics(events)
        self._svc._synthesis_event_observer = self._svc._synthesis.observe_event
        events.subscribe(
            self._svc._synthesis_event_observer,
            event_types={"CapabilityCompleted", "VerificationCompleted"},
        )

        # 12b. Register schedule capability AFTER the scheduler is constructed
        # (P1-25: ScheduleAPI must capture a live scheduler, not None).
        scheduler = Scheduler(
            store=schedules,
            task_manager=task_manager,
            admission=self._svc.require_task_ready,
            intake=self._svc.submit_spec,
            max_concurrent=cfg.scheduler_max_concurrent,
            loop_interval_seconds=cfg.scheduler_interval_seconds,
        )
        self._svc._scheduler = scheduler
        events.subscribe(scheduler.notify_event, exclude_event_types=FAST_EVENT_TYPES)
        schedule_api = ScheduleAPI(scheduler, task_manager)
        registry.register(ScheduleCapability(schedule_api))
        # Maintenance contracts are rehydrated before the core capability
        # bundle finishes registering. Create the live watcher owner first so
        # durable contracts can reconnect to the same observer surface rather
        # than silently degrading to scheduler-only polling.
        from athena.capabilities.watch import WatchRegistry

        if getattr(self._svc, "_watch_registry", None) is None:
            self._svc._watch_registry = WatchRegistry(
                observer_runner=self._svc._run_watch_observer,
            )
        from athena.capabilities.maintain import MaintenanceCapability

        maintenance = MaintenanceCapability(
            schedule_api,
            watch_registry=getattr(self._svc, "_watch_registry", None),
            workspace=workspace,
            execution_manager=self._svc._execution,
            fabric=self._svc._fabric,
            principal_id=cfg.cache_namespace,
        )
        registry.register(maintenance)
        try:
            restored = await maintenance.rehydrate()
            if restored:
                _logger.info("rehydrated %d maintenance observers", restored)
        except Exception as exc:
            self._svc._watch_registry.record_rehydration_failure(
                exc, contract_id="maintenance_contracts"
            )
            _logger.warning("maintenance observer rehydration failed: %s", exc)

        # 12.5 Crash recovery: reconcile orphaned state before claiming new work.
        from athena.recovery.manager import RecoveryManager

        # A proven reality commit may have completed just before a process
        # stopped, leaving the task row non-terminal. Finish that saga before
        # generic RUNNING -> INTERRUPTED recovery can hide the proven result.
        completion_recovered = await coordinator.reconcile_startup(task_manager)
        if completion_recovered:
            _logger.info(
                "recovered proven reality completions: %d",
                completion_recovered,
            )

        recovery = RecoveryManager(
            task_store=tasks,
            mutation_store=mutations,
            execution_store=execution_store,
            runtime_session_store=runtime_sessions,
            execution_manager=execution,
            event_store=events,
        )
        recovery_result = await recovery.recover()
        self._svc._recovery_status = recovery_result.status.value
        self._svc._recovery_summary = dict(recovery_result.summary)
        self._svc._recovery_error = recovery_result.error
        if recovery_result.status.value not in {"healthy", "recovered"}:
            raise RuntimeError(
                "service startup aborted: durable recovery state is "
                f"{recovery_result.status.value}"
                + (f": {recovery_result.error}" if recovery_result.error else "")
            )
        if any(recovery_result.summary.values()):
            _logger.info("crash recovery reconciled: %s", recovery_result.summary)

        # Reconcile transaction ownership after the mutation ledger has
        # classified any in-flight effects, but before workers can route new
        # calls into a durable in-place candidate.
        transaction_recovered = await self._svc._reality_gate.reconcile_startup()
        if transaction_recovered:
            _logger.warning(
                "transactional work requires operator reconciliation: %d",
                transaction_recovered,
            )

        # Fusion branches have a separate durable batch boundary. A branch
        # interrupted while applying real-workspace mutations must be marked
        # recovery-required before workers can claim fresh work; never replay
        # or infer a partially applied speculative commit at startup.
        shadow_recovered = await self._svc.shadow_engine().reconcile_startup(events)
        if shadow_recovered:
            _logger.warning(
                "shadow branches require operator reconciliation: %d",
                shadow_recovered,
            )

        # External systems sit beyond Athena's transaction boundary.  Any
        # receipt left in APPLYING/VERIFYING/COMPENSATING belongs to an
        # interrupted operation whose remote outcome is unknown; reconcile it
        # before workers can issue another request.  Unlike an observational
        # startup metric, failure here must abort startup fail-closed.
        external_recovered = await self._svc._external_effect_store.reconcile_startup()
        if external_recovered:
            _logger.warning(
                "external effects require operator reconciliation: %d",
                len(external_recovered),
            )
            for receipt in external_recovered:
                recovery_evidence = dict((receipt.get("response") or {}).get("recovery") or {})
                await events.append_event(
                    "ExternalEffectRecoveryRequired",
                    recovery_evidence,
                    task_id=receipt.get("task_id"),
                )

        # 12.75 Durable approval recovery: a resolved continuation is not
        # ordinary queued work. It belongs to a task that was already parked
        # in WAITING_APPROVAL, so the worker would never claim it. Recover the
        # exact task before the worker starts and let the kernel consume the
        # canonical call without asking the model to reproduce it.
        await self._svc._recover_approved_continuations(
            continuations=continuations,
            task_store=tasks,
            task_manager=task_manager,
            kernel=kernel,
        )

        # 12.76 Durable input-request recovery: a WAITING_INPUT task whose
        # answer arrived while the process was down. The answer is durable
        # (ANSWERED_PENDING_RESUME); the old kernel coroutine is not.
        if self._svc._store_input_requests is not None:
            await self._svc._recover_answered_input_requests(
                input_requests=self._svc._store_input_requests,
                task_store=tasks,
                task_manager=task_manager,
                kernel=kernel,
            )

        # 13. MCP (best-effort).
        self._svc._mcp = MCPAdapter(registry)
        await self._svc._connect_mcp()

        # Packs are rehydrated only after every native capability, durable
        # generated overlay, and configured MCP surface is available. This
        # lets declarative aliases and MCP contributions enter the same live
        # fabric on startup as they do during runtime installation.
        if self._svc._pack_manager is not None:
            self._svc._pack_manager.bind_integrations(
                skill_lifecycle=skill_lifecycle,
                workflow_store=self._svc._workflow_store,
                fabric=self._svc._fabric,
                dispatcher=self._svc._dispatcher,
                mcp_adapter=self._svc._mcp,
                mcp_client_sink=self._svc._mcp_clients.append,
            )
            try:
                activated = await self._svc._pack_manager.rehydrate_enabled()
                failures = self._svc._pack_manager.rehydration_failures()
                unavailable = {str(item["pack_id"]) for item in failures}
                quarantined = await self._svc._quarantine_tasks_for_packs(
                    task_store=tasks,
                    task_manager=task_manager,
                    unavailable=unavailable,
                )
                self._svc._startup_health["checks"]["enabled_packs"] = {
                    "status": "degraded" if failures else "ok",
                    "blocking": False,
                    "activated": activated,
                    "failures": failures,
                    "quarantined_tasks": quarantined,
                }
            except Exception as exc:
                _logger.warning("enabled capability-pack rehydration failed: %s", exc)
                self._svc._startup_health["checks"]["enabled_packs"] = {
                    "status": "degraded",
                    "blocking": False,
                    "error": str(exc),
                }

        # 14. Worker + scheduler. Packs and any dependent resumable tasks are
        # settled before a worker can claim fresh work.
        worker = TaskWorker(
            task_manager=task_manager,
            kernel=kernel,
            config=WorkerConfig(
                max_parallel=cfg.max_parallel_tasks,
                lease_duration_seconds=cfg.worker_lease_duration_seconds,
                lease_renewal_divisor=cfg.worker_lease_renewal_divisor,
            ),
        )
        self._svc._worker = worker
        task_manager.set_wakeup_callback(worker.notify)
        self._svc._worker_task = asyncio.create_task(self._svc._worker.run_forever())

        # 15. Start background scheduler loop.
        await scheduler.start()
        # Watch polling begins only after stores, capability registry, model
        # routing, recovery, packs, and MCP integrations are ready. A watcher
        # must never publish events into a half-constructed service.
        self._svc._watch_poll_task = asyncio.create_task(self._svc._poll_watches())
        self._svc._started = True
        degraded = any(
            value.get("status") != "ok"
            for value in self._svc._startup_health["checks"].values()
            if isinstance(value, dict)
        )
        self._svc._startup_health["status"] = "degraded" if degraded else "ok"
        self._svc._startup_health["blocking_failures"] = [
            name
            for name, value in self._svc._startup_health["checks"].items()
            if isinstance(value, dict) and value.get("blocking") and value.get("status") != "ok"
        ]

    async def stop(self) -> None:
        if not self._svc._started and self._svc._db is None:
            return

        # 1. Stop accepting/claiming new work first (P0-23).
        if self._svc._worker_task is not None:
            if self._svc._worker is not None:
                try:
                    await self._svc._worker.stop()
                except Exception as exc:
                    _logger.warning("worker stop failed: %s", exc)
            try:
                await self._svc._worker_task
            except Exception as exc:
                _logger.warning("worker task teardown failed: %s", exc)
            self._svc._worker_task = None

        # Approval recovery runs are not owned by TaskWorker, but they still
        # execute through the kernel and must not outlive service shutdown.
        # Cancelling the coroutine leaves the task recoverable; the normal
        # RUNNING -> INTERRUPTED pass below records that boundary.
        recovery_tasks = list(getattr(self._svc, "_approval_recovery_tasks", ()))
        for recovery in recovery_tasks:
            recovery.cancel()
        if recovery_tasks:
            await asyncio.gather(*recovery_tasks, return_exceptions=True)
        self._svc._approval_recovery_tasks.clear()

        # 2. Stop the scheduler (no new claims).
        if self._svc._scheduler is not None:
            try:
                await self._svc._scheduler.stop()
            except Exception as exc:
                _logger.warning("scheduler stop failed: %s", exc)
            if self._svc._store_events is not None:
                self._svc._store_events.unsubscribe(self._svc._scheduler.notify_event)
            self._svc._scheduler = None

        if self._svc._store_events is not None and self._svc._synthesis_event_observer is not None:
            self._svc._store_events.unsubscribe(self._svc._synthesis_event_observer)
            self._svc._synthesis_event_observer = None

        # 3. INTERRUPT active tasks (recoverable), never CANCEL (P0-23).
        #    Graceful shutdown parks in-flight work as INTERRUPTED so it can be
        #    resumed on next startup; only explicit user cancellation is a
        #    terminal CANCELLED. QUEUED tasks stay QUEUED and run next startup.
        if self._svc._store_tasks is not None and self._svc._task_manager is not None:
            try:
                rows = await self._svc._store_tasks.list_by_status(TaskStatus.RUNNING)
                for row in rows or []:
                    tid = row.get("id") if isinstance(row, dict) else getattr(row, "id", None)
                    if not tid:
                        continue
                    if self._svc._execution is not None:
                        try:
                            await self._svc._execution.cancel_task(tid)
                        except Exception as exc:
                            _logger.warning("cancel task %s on stop failed: %s", tid, exc)
                    try:
                        await self._svc._task_manager.transition(
                            tid, TaskStatus.INTERRUPTED, reason="service stopping"
                        )
                    except Exception as exc:
                        _logger.warning("interrupt task %s on stop failed: %s", tid, exc)
            except Exception as exc:
                _logger.warning("interrupt-running-tasks on stop failed: %s", exc)

        # Watch poller (P1-31): cancel and await before closing resources.
        poll_task = getattr(self._svc, "_watch_poll_task", None)
        if poll_task is not None:
            poll_task.cancel()
            try:
                await poll_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                _logger.warning("watch poller teardown failed: %s", exc)
            self._svc._watch_poll_task = None

        # Capability-owned resources via shutdown registry (P1-32).
        await self._svc._run_shutdown_hooks()
        self._svc._computer = None
        self._svc._browser = None
        self._svc._computer_health = {
            "state": "stopped",
            "backend": "unknown",
        }
        self._svc._browser_health = {
            "state": "stopped",
            "configured": False,
            "active_sessions": 0,
        }

        # MCP clients.
        for client in self._svc._mcp_clients:
            try:
                await client.close()
            except Exception as exc:
                _logger.warning("MCP client close failed: %s", exc)
        self._svc._mcp_clients = []
        for status in self._svc._mcp_connection_status.values():
            status["state"] = "stopped"
            status["tool_count"] = 0

        # External Hermes transport is optional and owns only its HTTP client.
        if self._svc._hermes_adapter is not None:
            try:
                await self._svc._hermes_adapter.aclose()
            except Exception as exc:
                _logger.warning("Hermes referee close failed: %s", exc)
            self._svc._hermes_adapter = None
            if self._svc._hermes_referee_owned:
                self._svc._hermes_referee = None
                self._svc._hermes_referee_owned = False

        # Runtimes / execution. ExecutionManager is the SOLE cleanup owner
        # (P0-2): its close_all covers task sessions, adopted execution
        # sessions, registered runtimes, and non-local backends. The service
        # must not reach into the manager's private runtime collection.
        if self._svc._execution is not None:
            try:
                outcome = await self._svc._execution.close_all()
            except Exception as exc:
                _logger.warning("execution close_all failed: %s", exc)
            else:
                if outcome.get("runtime_failures") or outcome.get("sessions_remaining"):
                    _logger.warning(
                        "execution shutdown incomplete: %d runtime failures, %d sessions remaining",
                        len(outcome.get("runtime_failures", ())),
                        len(outcome.get("sessions_remaining", ())),
                    )
            if self._svc._execution.live_resource_count() > 0:
                _logger.warning(
                    "execution manager still holds %d live resources after close_all",
                    self._svc._execution.live_resource_count(),
                )
            self._svc._execution = None

        # DB last.
        if self._svc._db is not None:
            if self._svc._store_events is not None:
                try:
                    await self._svc._store_events.close()
                except Exception as exc:
                    _logger.warning("event store close failed: %s", exc)
            if self._svc._fabric is not None:
                try:
                    await self._svc._fabric.flush()
                except Exception as exc:
                    _logger.warning("generated capability flush failed: %s", exc)
            try:
                await self._svc._db.close()
            except Exception as exc:
                _logger.warning("db close failed: %s", exc)
            self._svc._db = None

        self._svc._cancellations = None
        self._svc._world_state_store = None
        self._svc._project_index_store = None
        self._svc._project_index_builder = None
        self._svc._project_index_coordinator = None
        self._svc._failure_memory = None
        self._svc._generated_store = None
        self._svc._workflow_store = None
        self._svc._workflow_run_store = None
        self._svc._research_store = None
        self._svc._context_block_store = None
        self._svc._pack_store = None
        self._svc._pack_manager = None
        self._svc._skill_lifecycle = None
        self._svc._delegate_session_store = None
        self._svc._external_delegate_manager = None
        self._svc._capability_health_store = None
        self._svc._capability_health = None
        self._svc._synthesis = None
        self._svc._world_states = {}
        self._svc._started = False


def _body_observation_from_screen_event(event, payload: dict):
    """Convert a RuntimeScreenChanged event into a typed interpreter
    observation (P1-15). Returns None when the event carries no screen
    render (defensive: the capability announces the size, the full text
    comes from a bounded screen read the bridge performs itself).
    """
    from athena.interpreter.protocol import (
        BodyObservationKind,
        InterpreterObservation,
    )

    session_id = str(payload.get("session") or "")
    if not session_id:
        return None
    return InterpreterObservation(
        kind=BodyObservationKind.TERMINAL_SCREEN_CHANGED,
        payload={
            "session": session_id,
            "screen_chars": int(payload.get("screen_chars") or 0),
            "rows": payload.get("rows"),
            "cols": payload.get("cols"),
        },
        task_id=getattr(event, "task_id", None),
        session_id=getattr(event, "session_id", None),
        runtime_session_id=session_id,
    )
