"""Startup composition of model, context, verification, and kernel authorities."""

from __future__ import annotations

from typing import Any

from athena.context.compiler import ContextCompiler
from athena.state.context_digests import ContextDigestStore
from athena.kernel.kernel import AgentKernel
from athena.kernel.termination import TerminationEvaluator
from athena.models.registry import ProviderRegistry
from athena.service.observations import bind_body_observation_bridge
from athena.tasks.delegation import DelegationManager


async def build_reasoning_components(
    lifecycle: Any,
    *,
    cfg: Any,
    components: Any,
    execution: Any,
    authorities: Any,
) -> dict[str, Any]:
    ports = lifecycle.ports
    db = components.db
    events = components.events
    tasks = components.tasks
    messages = components.messages
    continuations = components.continuations
    input_requests = components.input_requests

    model_registry = ProviderRegistry()
    ports.register_providers(model_registry)
    ports.model_registry = model_registry
    from athena.voice import VoiceManager

    ports.voice = VoiceManager(
        model_registry,
        cfg.voice,
        ports.artifacts,
    )
    memory = authorities.memory
    skills_store = authorities.skills
    fabric = authorities.fabric
    budgets = authorities.budgets
    task_manager = authorities.task_manager
    cancellations = authorities.cancellations
    provider_readiness = model_registry.readiness()
    if provider_readiness.get("state") == "ready":
        ports.startup_health["checks"]["model_provider"] = {
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
        ports.startup_health["checks"]["model_provider"] = model_provider_check
    router = ports.build_model_router(model_registry, cfg)
    ports.router = router

    compiler = ContextCompiler(
        message_store=messages,
        memory_store=memory,
        skill_loader=skills_store,
        capability_registry=fabric,
        artifact_store=ports.artifacts,
        research_store=ports.research_store,
        workflow_store=ports.workflow_store,
        context_block_store=ports.context_block_store,
        context_digest_store=ContextDigestStore(db),
        summarizer=ports.make_model_summarizer(model_registry),
        context_window=cfg.context_window,
        reserve_output=cfg.reserve_output,
        principal_id=cfg.cache_namespace,
        workspace_reader=ports.workspace_reader(),
    )
    ports.compiler = compiler

    verifier = ports.build_verifier(
        execution=execution,
        dispatcher=ports.dispatcher,
        artifact_store=ports.artifacts,
        capability_registry=fabric,
        model_registry=router,  # ModelRouter: judge role routing
        evidence_provider=ports.verification_evidence,
        inference_broker=ports.make_judge_broker(),
    )
    ports.acceptance_verifier = verifier
    from athena.reality import RealityCoordinator, ShadowCandidateVerifier

    coordinator = RealityCoordinator(
        shadow_engine=ports.shadow_engine(),
        reality_gate=ports.reality_gate,
        candidate_verifier=ShadowCandidateVerifier(verifier),
        event_sink=ports.forward_events(events),
        default_criteria_source=ports.project_profile_for_completion,
        project_index_provider=ports.project_index_for_completion,
    )
    ports.reality_coordinator = coordinator
    from athena.service.workflow_runner import ServiceWorkflowRunner

    workflow_runner = ServiceWorkflowRunner(
        workflow_store=ports.workflow_store,
        fabric=ports.fabric,
        dispatch_factory=ports.dispatch_factory,
        run_store=ports.workflow_run_store,
        dispatcher=ports.dispatcher,
    )
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
            required_child_state=task_manager.required_child_state,
            defer_reality_verification=lambda task: (
                ports.reality_gate.active_branch(task.id) is not None
                or ports.reality_gate.checkpoint_id(task.id) is not None
            ),
        ),
        dispatch_factory=ports.dispatch_factory,
        continuation_store=continuations,
        workflow_run_store=ports.workflow_run_store,
        input_request_store=input_requests,
        steering_store=ports.steering_store,
        parked_slot_wait_s=cfg.parked_slot_wait_s,
        provider_usage_store=ports.provider_usage_store,
        model_response_store=ports.model_response_store,
        interpreter=ports.make_interpreter(),
        reality_coordinator=coordinator,
        secret_manager=ports.secrets,
        workflow_store=ports.workflow_store,
        workflow_fabric=ports.fabric,
        workflow_runner=workflow_runner,
    )
    ports.kernel = kernel

    ports.observation_callbacks = bind_body_observation_bridge(
        ports.background_tasks,
        events,
        kernel,
    )

    delegation = DelegationManager(
        task_manager=task_manager,
        kernel=kernel,
        budgets=budgets,
        cancellations=cancellations,
        steering_store=ports.steering_store,
        execution_manager=execution,
        principal_id=cfg.cache_namespace,
    )
    ports.delegation = delegation

    from athena.service.resource_finalizer import (
        TaskResourceFinalizer,
        TaskResourceRetentionPolicy,
    )

    finalizer = TaskResourceFinalizer(
        event_sink=ports.forward_events(events),
        retention_policy=TaskResourceRetentionPolicy(
            mode=cfg.parked_resource_retention_mode,
            retain_seconds=cfg.parked_resource_retain_seconds,
        ),
    )
    finalizer.bind_obligation_store(ports.resource_obligation_store)
    finalizer.bind_service(lifecycle.ports.owner)
    ports.resource_finalizer = finalizer
    kernel.set_parked_resource_releaser(finalizer.release_parked)
    kernel.set_parked_resource_resumption_handler(finalizer.cancel_parked_release)
    task_manager.set_finalization_barrier(finalizer.quiesce)
    task_manager.add_finalize_observer(finalizer.finalize)
    _dispatcher = ports.dispatcher

    async def _release_dispatch_state(task: Any, _result: Any) -> None:
        del _result
        if _dispatcher is not None:
            _dispatcher.release_task_sync(str(getattr(task, "id", "") or ""))

    task_manager.add_finalize_observer(_release_dispatch_state)
    from athena.service.budget_cleanup import BudgetFamilyCleanupObserver

    task_manager.add_finalize_observer(
        BudgetFamilyCleanupObserver(budgets, task_store=components.tasks)
    )
    # Durable complexity facts are reconstructed from canonical events after a
    # restart; the dispatcher never derives them from prose or model claims.
    if _dispatcher is not None:
        _dispatcher.set_event_source(events)
    return {
        "model_registry": model_registry,
        "router": router,
        "compiler": compiler,
        "verifier": verifier,
        "coordinator": coordinator,
        "kernel": kernel,
        "finalizer": finalizer,
    }
