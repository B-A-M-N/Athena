"""Fusion orchestrator: the three engines operating as ONE system.

Ties together the pieces so their combination is greater than the parts:

    ShadowEngine   (speculative execution in isolated clones)
    TaskWorldState (claims + evidence + invariants + machine reality)
    TaskForker     (causal forks from any event point)
    CheckpointManager (workspace snapshots)
    SynthesisEngine (ephemeral capabilities -> proof-carrying skills)

Integrated workflows this module enables:

1. SpeculativeExperiment.run()  — propose ops, execute in shadow, verify,
   record a CLAIM bound to the evidence, check INVARIANTS before commit,
   commit only if the envelope holds; on failure, fork the parent task
   from the pre-experiment event for an alternate approach.

2. Invariant-gated commits — no shadow branch can commit while a required
   invariant is violated; violations are recorded as world-state facts.

3. Claim invalidation on commit — committing a branch invalidates STALE
   every claim whose dependency paths overlap the committed files.

4. Proof-carrying synthesis — synthetic capabilities validated in shadow
   branches carry their branch id as provenance; repeated success converts
   them to SkillCandidates with that evidence attached.

5. Fork-with-checkpoint — forking optionally captures a checkpoint of the
   parent workspace first, so the fork can restore to the exact causal
   state rather than merely replaying events.

Nothing here bypasses the kernel/policy/executor path; everything is
durable and auditable through the canonical event log.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from athena.causal.checkpoint import CheckpointManager
from athena.fusion.comparison import ComparisonLifecycle
from athena.fusion.ports import FusionPorts
from athena.fusion.selection import CandidateSelectionStore
from athena.causal.fork import TaskForker
from athena.protocol.capabilities import CapabilityRequestOrigin
from athena.protocol.failure import RecoveryDiagnostic
from athena.protocol.ids import new_id

__all__ = ["ExperimentResult", "FusionOrchestrator"]

_logger = logging.getLogger("athena.fusion")


@dataclass
class ExperimentResult:
    """Outcome of one speculative experiment."""

    branch_id: str = ""
    status: str = "PROPOSED"  # COMMITTED | FAILED | DISCARDED
    claim_id: str | None = None  # claim recorded for a successful run
    invariant_report: dict = field(default_factory=dict)
    verification: list[dict] = field(default_factory=list)
    commit: dict = field(default_factory=dict)
    error: str | None = None
    fork_id: str | None = None  # set when auto-forking on failure
    verified: bool = False  # shadow criteria + invariant envelope passed
    elapsed_ms: float = 0.0
    changed_resources: tuple[str, ...] = ()
    failure_record: dict = field(default_factory=dict)


class FusionOrchestrator:
    """The integration layer binding all fusion engines together.

    Accepts an explicit ``AthenaService`` (legacy) or an explicit ports dict
    for decoupled construction. The ports dict is the preferred path because
    it removes the runtime coupling between Fusion and the whole service.
    """

    def __init__(
        self,
        service: Any = None,
        *,
        ports: dict[str, Any] | None = None,
        typed_ports: FusionPorts | None = None,
    ) -> None:
        self.ports: FusionPorts | None = typed_ports
        if typed_ports is not None:
            self.service = None
            self.shadow = typed_ports.shadow
            self.checkpoints = typed_ports.checkpoints
            self._store_tasks = typed_ports.task_store
            self._store_events = typed_ports.event_store
            self._store_runtime_sessions = typed_ports.runtime_session_store
            self._context_block_store = typed_ports.context_block_store
            self._fabric = typed_ports.fabric
            self._workflow_store = typed_ports.workflow_store
            self._synthesis = typed_ports.synthesis
            self._dispatcher = typed_ports.dispatcher
            self._world_state_store = typed_ports.world_state_store
            self._default_workspace = typed_ports.default_workspace
            self._world_state_factory = typed_ports.world_state_factory
            self._synthesis_ref = typed_ports.synthesis_ref
            fork_ports = typed_ports.fork
            self.forker = TaskForker(ports=fork_ports, checkpoint_manager=self.checkpoints)
        elif ports is not None:
            self.service = None
            self.shadow = ports["shadow"]
            self.checkpoints = ports["checkpoints"]
            self.forker = TaskForker(
                service=ports.get("service"), checkpoint_manager=self.checkpoints
            )
            self._store_tasks = ports.get("store_tasks")
            self._default_workspace = ports.get("default_workspace")
            self._world_state_factory = ports.get("world_state_factory")
            self._synthesis_ref = ports.get("synthesis_ref")
        else:
            if service is None:
                raise ValueError("FusionOrchestrator requires service or ports")
            self.ports = None
            self.service = service
            self.shadow = service.shadow_engine()
            state_root = getattr(service, "_runtime_state_root", None)
            service_checkpoints = getattr(service, "_checkpoints", None)
            if service_checkpoints is not None:
                self.checkpoints = service_checkpoints
            else:
                self.checkpoints = CheckpointManager(
                    root=(
                        os.path.join(state_root, "checkpoints")
                        if state_root
                        else "/tmp/athena-checkpoints"
                    )
                )
            self.forker = TaskForker(service=service, checkpoint_manager=self.checkpoints)
            self._store_tasks = None
            self._default_workspace = None
            self._world_state_factory = None
            self._synthesis_ref = None
        state_root = str(getattr(self.shadow, "_state_root", "") or "/tmp/athena-fusion-selection")
        self.selection_store = CandidateSelectionStore(state_root)

    async def _task_for(self, task_id: str):
        """Resolve a TaskSpec from the ports task store or the legacy service."""
        store = self._store_tasks
        if store is None:
            store = getattr(self.service, "_store_tasks", None) if self.service else None
        if task_id and store:
            try:
                row = await store.get(task_id)
                if row:
                    from athena.kernel.lifecycle import deserialize_task

                    return deserialize_task(dict(row))
            except Exception as exc:  # noqa: BLE001
                _logger.warning("task lookup for %s failed: %s", task_id, exc)
        return None

    def _workspace_for_async(self, task_id: str):
        """Legacy workspace resolution for the service path."""
        return self._workspace_for(task_id)

    def _world_state(self, task_id: str):
        """Resolve world state from ports or legacy service."""
        if self._world_state_factory is not None:
            return self._world_state_factory(task_id)
        if self.service is not None:
            return self.service.world_state(task_id)
        raise RuntimeError("FusionOrchestrator has no service bound for world_state")

    # ------------------------------------------------------------------
    # 1+3+4: speculative experiment with invariant gate and claims
    # ------------------------------------------------------------------
    async def run_experiment(
        self,
        *,
        task_id: str,
        proposal: list[dict],
        invariants: list[dict] | None = None,
        profile: str | None = None,
        auto_fork_on_failure: bool = True,
    ) -> ExperimentResult:
        """Run one full speculative experiment against reality.

        Fusion terminal success is always ``CANDIDATE_READY``; promotion
        belongs to the canonical candidate/reality path.
        """
        task = await self._task_for(task_id)
        ws = (
            task.workspace
            if task is not None and task.workspace is not None
            else (
                getattr(self.service, "_default_workspace", None)
                if self.service
                else self._default_workspace
            )
        )
        result = ExperimentResult()
        started = time.monotonic()
        pre_experiment_sequence = 0
        try:
            timeline = await self.forker.timeline(task_id)
            pre_experiment_sequence = max((e["sequence"] for e in timeline), default=0)
        except Exception as exc:  # noqa: BLE001 - timeline lookup is best-effort before experiment
            _logger.warning("pre-experiment timeline failed: %s", exc)

        ckpt = await self.capture_checkpoint(
            task_id=task_id or "unknown",
            workspace_root=(ws.root if ws else ""),
            label="pre-experiment",
        )
        ckpt_id = ckpt.get("checkpoint_id") or ckpt.get("id")
        _logger.info("experiment checkpoint %s", ckpt_id)

        checkpoint_owner = task_id or "unknown"
        branch = await self.shadow.open_branch(
            task_id=task_id, base_workspace=ws, proposal=proposal, profile=profile
        )
        branch_checkpoint_owner = f"branch:{branch.id}"
        if ckpt_id:
            self.checkpoints.retain(ckpt_id, owner=branch_checkpoint_owner)
        self.shadow.attach_checkpoint(branch, ckpt_id)
        result.branch_id = branch.id

        branch = await self.shadow.execute_branch(branch, profile=profile)
        if branch.status == "FAILED":
            result.status = "FAILED"
            result.error = branch.error
            await self._attach_failure_record(result, proposal, task, branch)
            result.elapsed_ms = round((time.monotonic() - started) * 1000, 3)
            await self._fail_path(
                task_id, result, auto_fork_on_failure, ckpt_id, pre_experiment_sequence
            )
            await self._close_experiment_checkpoint(
                ckpt_id,
                task_owner=checkpoint_owner,
                branch_owner=branch_checkpoint_owner,
                state="FAILED",
            )
            return result

        canonical_verifier = (
            self.ports.candidate_verifier
            if self.ports is not None and self.ports.candidate_verifier is not None
            else getattr(self.service, "_reality_coordinator", None)
        )
        if task is None or canonical_verifier is None:
            verification = [
                {
                    "id": "verification_unavailable",
                    "passed": False,
                    "reason": "no candidate-verification port composed",
                }
            ]
            criteria_present = False
            all_ok = False
        else:
            gate = (
                self.ports.reality_gate
                if self.ports is not None and self.ports.reality_gate is not None
                else getattr(self.service, "_reality_gate", None)
            )
            deactivate = getattr(gate, "deactivate_branch", None)
            if (
                callable(deactivate)
                and gate is not None
                and task_id
                and gate.active_branch(task_id) is branch
            ):
                await deactivate(task_id)
            diff = getattr(self.shadow, "_diff_trees_async", None)
            changes = await diff(branch) if callable(diff) else {}
            changed_resources = tuple(
                sorted(
                    set(changes.get("modified", ()))
                    | set(changes.get("added", ()))
                    | set(changes.get("deleted", ()))
                )
            )
            result.changed_resources = changed_resources
            verifier_impact = getattr(
                canonical_verifier,
                "_candidate_verification",
                canonical_verifier,
            )
            impact = await verifier_impact.impact_for(
                (ws.root if ws else ""),
                changed_resources,
            )
            verification = await canonical_verifier.verify_candidate(
                task,
                workspace=branch.shadow_workspace,
                changed_resources=changed_resources,
                impact=impact,
                profile_workspace=ws,
                deactivate_branch=deactivate,
                task_id=task_id,
            )
            real_proof = [
                item for item in verification if item.get("id") != "no_criteria_derivable"
            ]
            criteria_present = bool(real_proof)
            all_ok = criteria_present and all(item.get("passed") for item in real_proof)
        invariant_report: dict = {}
        if all_ok:
            invariant_set = self._build_invariants(
                invariants,
                branch=branch,
                profile=profile,
                task_id=task_id,
            )
            invariant_report = await invariant_set.check_all()
            result.invariant_report = invariant_report
            if not invariant_report.get("ok", False):
                result.status = "FAILED"
                result.error = "invariant violation: " + str(
                    invariant_report.get("violations")
                    or invariant_report.get("failed")
                    or "required invariant failed"
                )
                await self._attach_failure_record(result, proposal, task, branch)
                result.elapsed_ms = round((time.monotonic() - started) * 1000, 3)
                await self.shadow.discard(branch, reason=result.error)
                await self._fail_path(
                    task_id,
                    result,
                    auto_fork_on_failure,
                    ckpt_id,
                    pre_experiment_sequence,
                )
                await self._close_experiment_checkpoint(
                    ckpt_id,
                    task_owner=checkpoint_owner,
                    branch_owner=branch_checkpoint_owner,
                    state="FAILED",
                )
                return result

        await self.shadow.record_verification(branch, verification)
        result.verification = verification
        if not all_ok:
            result.status = "FAILED"
            result.error = (
                "acceptance criteria failed in shadow"
                if criteria_present
                else "verification failed: no acceptance criteria were supplied"
            )
            await self._attach_failure_record(result, proposal, task, branch)
            result.elapsed_ms = round((time.monotonic() - started) * 1000, 3)
            await self.shadow.discard(branch, reason=result.error)
            await self._fail_path(
                task_id, result, auto_fork_on_failure, ckpt_id, pre_experiment_sequence
            )
            await self._close_experiment_checkpoint(
                ckpt_id,
                task_owner=checkpoint_owner,
                branch_owner=branch_checkpoint_owner,
                state="FAILED",
            )
            return result

        result.verified = True
        result.status = "CANDIDATE_READY"
        result.elapsed_ms = round((time.monotonic() - started) * 1000, 3)
        await self._close_experiment_checkpoint(
            ckpt_id,
            task_owner=checkpoint_owner,
            branch_owner=branch_checkpoint_owner,
            state="CANDIDATE_READY",
        )
        return result

    async def _attach_failure_record(self, result, proposal, task, branch) -> None:
        """Attach typed evidence for kernel-owned recovery decisions."""
        budget = getattr(task, "resource_budget", None) if task is not None else None
        budget_record = None
        budget_tracker = getattr(self.service, "_budgets", None) if self.service else None
        remaining = None
        if budget_tracker is not None and task is not None:
            remaining_fn = getattr(budget_tracker, "remaining", None)
            if callable(remaining_fn):
                try:
                    remaining = dict(await remaining_fn(task.id))
                except (OSError, RuntimeError, TypeError, ValueError):
                    remaining = None
        if budget is not None:
            to_record = getattr(budget, "to_record", None)
            if callable(to_record):
                budget_record = to_record()
            elif hasattr(budget, "__dict__"):
                budget_record = dict(vars(budget))
        diagnostic = RecoveryDiagnostic(
            operation="fusion.speculative_proposal",
            failure_class="speculative_verification_failure",
            verification=tuple(item for item in result.verification if isinstance(item, dict)),
            resource_constraints=remaining or budget_record or {},
            permitted_recovery=(
                "propose_materially_different_candidate",
                "reverify_against_canonical_acceptance",
                "stop_if_no_meaningful_alternative_or_budget",
            ),
            evidence={"branch_id": result.branch_id},
        ).as_dict()
        result.failure_record = {
            "diagnostic": diagnostic,
            "kind": "speculative_failure",
            "failed_operation": [dict(item) for item in proposal],
            "error": result.error,
            "verification_results": list(result.verification),
            "violated_invariants": list(
                result.invariant_report.get("violations")
                or result.invariant_report.get("failed")
                or []
            ),
            "workspace_changes": list(result.changed_resources),
            "remaining_execution_budget": remaining or budget_record,
            "branch_id": result.branch_id,
            "recovery_actions": [
                "propose_materially_different_candidate",
                "reverify_against_canonical_acceptance",
                "stop_if_no_meaningful_alternative_or_budget",
            ],
        }

    async def compare(
        self,
        *,
        task_id: str,
        proposals: list[list[dict]],
        invariants: list[dict] | None = None,
        profile: str | None = None,
    ) -> dict[str, Any]:
        """Run bounded alternatives; retained proof feeds exact selection."""
        return await ComparisonLifecycle(self).compare(
            task_id=task_id,
            proposals=proposals,
            invariants=invariants,
            profile=profile,
        )

    async def select_candidate(self, comparison_id: str, branch_id: str) -> dict:
        """Select one verified candidate and discard verified losers."""
        return await ComparisonLifecycle(self).select_candidate(comparison_id, branch_id)

    # ------------------------------------------------------------------
    # 2+5: fork with checkpoint restoration context
    # ------------------------------------------------------------------
    async def fork_from_event(
        self,
        *,
        task_id: str,
        after_event_sequence: int,
        capture_checkpoint: bool = False,
        checkpoint_id: str | None = None,
    ) -> dict:
        """Fork a task from a causal point, optionally checkpointing first."""
        ckpt = None
        if capture_checkpoint:
            ws = await self._workspace_for(task_id)
            ckpt = await self.capture_checkpoint(
                task_id=task_id, workspace_root=ws.root, label=f"fork-after-{after_event_sequence}"
            )
            checkpoint_id = ckpt.get("checkpoint_id") or ckpt.get("id")
        outcome = await self.forker.fork(
            task_id=task_id,
            after_event_sequence=after_event_sequence,
            workspace_checkpoint_id=checkpoint_id,
        )
        if checkpoint_id:
            outcome["checkpoint_id"] = checkpoint_id
        return outcome

    async def capture_checkpoint(
        self,
        *,
        task_id: str,
        workspace_root: str,
        label: str,
    ) -> dict[str, Any]:
        """Capture workspace files plus the semantic state at that boundary.

        The file snapshot remains the restore authority.  The attached
        semantic envelope is deliberately descriptive: it lets a restart,
        fork, or operator understand which task/world/context/runtime state
        was true when the snapshot was made without claiming that a live
        process can be restored from JSON.
        """
        semantic = await self._semantic_snapshot(
            task_id=task_id,
            workspace_root=workspace_root,
        )
        return await self.checkpoints.capture(
            task_id=task_id or "unknown",
            workspace_root=workspace_root,
            label=label,
            metadata={
                "type": "semantic_state_checkpoint",
                "version": 1,
                "captured_at": semantic.pop("captured_at"),
                "state": semantic,
            },
        )

    async def _semantic_snapshot(
        self,
        *,
        task_id: str,
        workspace_root: str,
    ) -> dict[str, Any]:
        """Build a bounded, JSON-safe semantic state envelope."""
        task: dict[str, Any] | None = None
        store_tasks = getattr(self.service, "_store_tasks", None)
        if store_tasks is not None:
            try:
                row = await store_tasks.get(task_id)
                if row is not None:
                    task = {
                        key: row.get(key)
                        for key in (
                            "id",
                            "status",
                            "objective",
                            "session_id",
                            "parent_task_id",
                            "acceptance_criteria",
                            "context_refs",
                            "workspace",
                            "capability_policy",
                            "model_policy",
                            "resource_budget",
                            "deadline",
                            "delivery",
                        )
                        if key in row
                    }
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                _logger.warning("semantic checkpoint task snapshot failed: %s", exc)

        events: dict[str, Any] = {"last_sequence": 0, "recent_types": []}
        event_store = getattr(self.service, "_store_events", None)
        if event_store is not None:
            try:
                timeline = await event_store.list_for_task(task_id)
                events = {
                    "last_sequence": max(
                        (int(event.sequence or 0) for event in timeline),
                        default=0,
                    ),
                    "recent_types": [str(event.type) for event in timeline[-20:]],
                }
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                _logger.warning("semantic checkpoint event snapshot failed: %s", exc)

        world_state: dict[str, Any] = {}
        world_state_provider = getattr(self.service, "world_state", None)
        if callable(world_state_provider):
            try:
                world_state = await world_state_provider(task_id).snapshot(
                    workspace_root=workspace_root
                )
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                _logger.warning("semantic checkpoint world snapshot failed: %s", exc)

        runtimes: list[dict[str, Any]] = []
        runtime_store = getattr(self.service, "_store_runtime_sessions", None)
        if runtime_store is not None:
            try:
                for row in await runtime_store.list_for_task(task_id):
                    runtimes.append(
                        {
                            key: row.get(key)
                            for key in (
                                "id",
                                "backend",
                                "runtime",
                                "cwd",
                                "pid",
                                "is_alive",
                                "started_at",
                                "last_heartbeat",
                                "ended_at",
                            )
                            if key in row
                        }
                    )
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                _logger.warning("semantic checkpoint runtime snapshot failed: %s", exc)

        contexts: list[dict[str, Any]] = []
        context_store = getattr(self.service, "_context_block_store", None)
        if context_store is not None:
            try:
                blocks = await context_store.list(
                    scopes=(("task", task_id),),
                    attached_only=False,
                )
                contexts = [
                    {
                        "id": block.id,
                        "version": block.version,
                        "label": block.label,
                        "scope": block.scope,
                        "scope_id": block.scope_id,
                        "attached": block.attached,
                    }
                    for block in blocks
                ]
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                _logger.warning("semantic checkpoint context snapshot failed: %s", exc)

        affordances: dict[str, Any] = {"capabilities": [], "workflows": []}
        fabric = getattr(self.service, "_fabric", None)
        if fabric is not None:
            try:
                affordances["capabilities"] = [
                    {
                        "id": record.get("id"),
                        "scope": record.get("scope"),
                        "lifecycle_state": record.get("lifecycle_state"),
                        "code_hash": record.get("code_hash"),
                        "schema_hash": record.get("schema_hash"),
                    }
                    for record in fabric.created_this_task(task_id)
                ]
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                _logger.warning("semantic checkpoint affordance snapshot failed: %s", exc)
        workflow_store = getattr(self.service, "_workflow_store", None)
        if workflow_store is not None:
            try:
                workflows = await workflow_store.list(task_id=task_id)
                affordances["workflows"] = [
                    {
                        "id": workflow.id,
                        "version": workflow.version,
                        "scope": workflow.scope.value,
                        "lifecycle_state": workflow.lifecycle_state,
                        "step_count": len(workflow.steps),
                    }
                    for workflow in workflows
                ]
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                _logger.warning("semantic checkpoint workflow snapshot failed: %s", exc)

        branches = []
        try:
            branches = [
                {
                    "id": branch.id,
                    "status": branch.status,
                    "commit_state": branch.commit_state,
                    "checkpoint_id": branch.checkpoint_id,
                }
                for branch in self.shadow.list_branches()
                if branch.task_id == task_id
            ]
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            _logger.warning("semantic checkpoint branch snapshot failed: %s", exc)

        from athena.protocol.messages import utcnow

        return {
            "captured_at": utcnow().isoformat(),
            "task": task,
            "event_boundary": events,
            "world_state": world_state,
            "attached_context": contexts,
            "runtime_sessions": runtimes,
            "affordances": affordances,
            "shadow_branches": branches,
        }

    async def synthesize_from_branch(
        self,
        registry,
        *,
        name: str,
        description: str,
        code: str,
        input_schema: dict,
        effects: set,
        task_id: str | None,
        validation_cases: list[dict],
    ) -> dict:
        """Synthesize a capability validated INSIDE a shadow context.

        The branch provenance becomes part of the proof carried by the
        resulting skill candidate.
        """
        from athena.synthesis.engine import SynthesisEngine

        engine = getattr(self.service, "_synthesis", None)
        if engine is None:
            engine = SynthesisEngine()
            # Services create the shared engine during startup. This fallback
            # keeps the orchestrator usable with lightweight test doubles.
            if self.service is not None:
                if hasattr(self, "_synthesis_ref") and self._synthesis_ref is not None:
                    self._synthesis_ref.set(engine)
                else:
                    self.service._synthesis = engine
        dispatcher = getattr(self.service, "_dispatcher", None) if self.service else None
        engine.bind_dispatcher(dispatcher)

        cap = engine.synthesize(
            name=name,
            description=description,
            code=code,
            input_schema=input_schema,
            effects=effects,
            task_id=task_id,
            provenance={"origin": "shadow_experiment"},
        )
        cap = await engine.validate(cap, validation_cases)
        # Generated machinery belongs in the effective task overlay.  Falling
        # back to the supplied global registry remains supported for older
        # callers/tests, but the service path never exposes it globally.
        surface = getattr(self.service, "_fabric", None) or registry
        admitted = engine.register_ephemeral(surface, cap)
        candidate = engine.to_skill_candidate(cap.id) if cap.validation.get("all_passed") else None
        return {
            "capability_id": cap.id,
            "admitted": admitted,
            "validation": cap.validation,
            "proof": engine.proof_for(cap.id),
            "skill_candidate_proposed": candidate is not None,
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    async def _close_experiment_checkpoint(
        self,
        checkpoint_id: str | None,
        *,
        task_owner: str,
        branch_owner: str,
        state: str,
        keep_branch: bool = False,
        claim_id: str | None = None,
    ) -> None:
        """Close experiment checkpoint owners without deleting live evidence.

        A failed comparison is disposable, while a conflicted or recovery
        candidate must retain its exact pre-experiment workspace.  Successful
        experiments transfer that retention to the claim that cites the
        checkpoint.  The transfer is explicit so a compare loop cannot leak
        one snapshot per candidate or silently delete proof still in use.
        """
        if not checkpoint_id:
            return
        try:
            self.checkpoints.mark_terminal(checkpoint_id, state=state)
            if claim_id:
                self.checkpoints.retain(checkpoint_id, owner=f"claim:{claim_id}")
            if not keep_branch:
                await self.checkpoints.release(checkpoint_id, owner=branch_owner)
            await self.checkpoints.release(checkpoint_id, owner=task_owner)
        except Exception as exc:  # noqa: BLE001 - result must remain inspectable
            _logger.warning(
                "experiment checkpoint owner cleanup failed for %s: %s",
                checkpoint_id,
                exc,
            )

    async def _task_for_legacy(self, task_id: str):
        store = self._store_tasks
        if store is None and self.service:
            store = getattr(self.service, "_store_tasks", None)
        if task_id and store:
            try:
                row = await store.get(task_id)
                if row:
                    from athena.kernel.lifecycle import deserialize_task

                    return deserialize_task(dict(row))
            except Exception as exc:  # noqa: BLE001 - workspace fallback keeps orchestration available
                _logger.warning("task lookup for %s failed: %s", task_id, exc)
        return None

    async def _workspace_for(self, task_id: str):
        task = await self._task_for(task_id)
        if task is not None and task.workspace is not None:
            return task.workspace
        if self._default_workspace is not None:
            return self._default_workspace
        if self.service is not None:
            return getattr(self.service, "_default_workspace", None)
        raise RuntimeError("FusionOrchestrator has no service bound for default workspace")

    async def _dispatch_probe(
        self, code: str, workspace, profile: str | None, task_id: str | None = None
    ) -> tuple[bool, str]:
        """Run one probe through the capability/policy path (item 16).

        Dispatches an ``execute`` request bound to the given (shadow)
        workspace so probes pass policy like every other operation instead
        of bypassing via raw subprocess.
        """
        from athena.protocol.continuations import SuspendedCall
        from athena.protocol.capabilities import CapabilityRequest

        dispatcher = getattr(self.service, "_dispatcher", None)
        if dispatcher is None:
            raise RuntimeError("service has no capability dispatcher bound")
        req = CapabilityRequest(
            capability_id="execute",
            arguments={"language": "shell", "code": code},
            task_id=task_id,
            call_id=new_id("call"),
            origin=CapabilityRequestOrigin.TRUSTED_ORCHESTRATION,
        )
        result = await dispatcher.dispatch(req, workspace=workspace, profile=profile)
        if isinstance(result, SuspendedCall):
            return False, "probe requires approval; suspended"
        if isinstance(result, Exception):
            return False, str(result)
        ok = getattr(result.status, "value", str(result.status)) == "ok"
        detail = (getattr(result, "output", None) or "") + (getattr(result, "error", None) or "")
        return ok, detail

    def _rewrite_to_shadow(self, command: str, branch) -> str:
        """Rewrite real paths to the sandbox-visible shadow mount.

        Shadow verification runs through the ``shadow`` execution backend.
        Inside its mount namespace the branch root is ``/workspace``; the
        host-side temporary path is intentionally not visible.  Rewriting
        only to the host shadow directory makes an absolute probe look right
        in the parent process but fail inside the actual sandbox.
        """
        real_root = os.path.realpath(branch.base_workspace.root)
        shadow_root = os.path.realpath(branch.shadow_workspace.root)
        if real_root == shadow_root:
            return command
        return command.replace(real_root, "/workspace")

    def _build_invariants(
        self,
        specs: list[dict] | None,
        *,
        branch=None,
        profile: str | None = None,
        task_id: str | None = None,
    ):
        """Build an InvariantSet from declarative or callable specs.

        Declarative spec: {"description", "command"} — the probe runs that
        command inside the shadow workspace via the dispatcher (same
        capability/policy path as everything else).
        """
        from athena.worldstate import InvariantSet

        inv = InvariantSet(
            task_id=task_id,
            store=getattr(self.service, "_world_state_store", None),
        )
        for spec in specs or []:
            probe = spec.get("probe")
            if probe is not None:
                raise ValueError(
                    "fusion invariants must use declarative command specs; "
                    "arbitrary Python probes are not durable"
                )
            if probe is None and spec.get("command") and branch is not None:
                code = self._rewrite_to_shadow(spec["command"], branch)

                async def cmd_probe(cmd=code):
                    ok, _ = await self._dispatch_probe(
                        cmd, branch.shadow_workspace, profile, task_id=task_id
                    )
                    return ok

                probe = cmd_probe
            if probe is None:
                raise ValueError(f"invariant spec needs 'command' or 'probe': {spec!r}")
            inv.add(
                spec["description"],
                probe,
                definition={
                    "type": "command",
                    "command": spec.get("command"),
                    "required": bool(spec.get("required", True)),
                },
            )
        return inv

    async def _fail_path(
        self,
        task_id,
        result: ExperimentResult,
        auto_fork: bool,
        ckpt_id: str | None,
        pre_experiment_sequence: int | None = None,
    ) -> None:
        """On failure: discard handled by caller; optionally fork for retry.

        Forks branch from the PRE-EXPERIMENT event position (P1-40): the
        alternate approach must not inherit the failed experiment's events.
        """
        if not auto_fork or not task_id:
            return
        try:
            seq = pre_experiment_sequence
            if seq is None:
                timeline = await self.forker.timeline(task_id)
                seq = max((e["sequence"] for e in timeline), default=0)
            outcome = await self.fork_from_event(
                task_id=task_id,
                after_event_sequence=seq,
                capture_checkpoint=False,
                checkpoint_id=ckpt_id,
            )
            result.fork_id = outcome.get("fork_id")
            result.commit = {"checkpoint_available": ckpt_id}
            _logger.info(
                "experiment failed; forked %s -> %s for alternate approach", task_id, result.fork_id
            )
        except Exception as exc:  # noqa: BLE001 - auto-fork is recovery best-effort
            _logger.warning("auto-fork after failure failed: %s", exc)
