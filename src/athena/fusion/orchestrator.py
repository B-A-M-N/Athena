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
from athena.fusion.semantic_snapshot import SemanticSnapshot
from athena.fusion.branch_synthesis import BranchSynthesisMechanism
from athena.fusion.invariants import InvariantProbeMechanism
from athena.fusion.ports import FusionPorts
from athena.fusion.selection import CandidateSelectionStore
from athena.causal.fork import TaskForker
from athena.protocol.failure import RecoveryDiagnostic

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
            self._verification_environment = typed_ports.verification_environment
            self._verification_environment_resolver = typed_ports.verification_environment_resolver
            self._budgets = typed_ports.budget_provider
            self._world_state_store = typed_ports.world_state_store
            self._default_workspace = typed_ports.default_workspace
            self._world_state_factory = (
                typed_ports.world_state_provider or typed_ports.world_state_factory
            )
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
            self._synthesis = ports.get("synthesis")
            self._synthesis_ref = ports.get("synthesis_ref")
            self._dispatcher = ports.get("dispatcher")
            self._verification_environment = ports.get("verification_environment")
            self._verification_environment_resolver = ports.get("verification_environment_resolver")
            self._budgets = ports.get("budget_provider")
            self._world_state_store = ports.get("world_state_store")
            self._fabric = ports.get("fabric")
            self._workflow_store = ports.get("workflow_store")
            self._store_events = ports.get("event_store")
            self._store_runtime_sessions = ports.get("runtime_session_store")
            self._context_block_store = ports.get("context_block_store")
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
            self._store_tasks = getattr(service, "_store_tasks", None)
            self._default_workspace = getattr(service, "_default_workspace", None)
            self._world_state_factory = None
            self._synthesis = getattr(service, "_synthesis", None)
            self._synthesis_ref = None
            self._dispatcher = getattr(service, "_dispatcher", None)
            self._verification_environment = getattr(service, "_verification_environment", None)
            self._verification_environment_resolver = None
            self._budgets = getattr(service, "_budgets", None)
            self._world_state_store = getattr(service, "_world_state_store", None)
            self._fabric = getattr(service, "_fabric", None)
            self._workflow_store = getattr(service, "_workflow_store", None)
            self._store_events = getattr(service, "_store_events", None)
            self._store_runtime_sessions = getattr(service, "_store_runtime_sessions", None)
            self._context_block_store = getattr(service, "_context_block_store", None)
        if typed_ports is None and ports is None:
            self._reality_coordinator = getattr(service, "_reality_coordinator", None)
            self._reality_gate = getattr(service, "_reality_gate", None)
        elif typed_ports is not None:
            self._reality_coordinator = typed_ports.reality_coordinator
            self._reality_gate = typed_ports.reality_gate
        else:
            port_map = ports or {}
            self._reality_coordinator = port_map.get("reality_coordinator")
            self._reality_gate = port_map.get("reality_gate")
        state_root = str(getattr(self.shadow, "_state_root", "") or "/tmp/athena-fusion-selection")
        self.selection_store = CandidateSelectionStore(state_root)
        self._task_resolver = self._resolve_task
        self._world_state_provider = self._resolve_world_state
        self._configured_default_workspace = self._default_workspace
        self._default_workspace = self._fallback_workspace
        self._semantic_state = SemanticSnapshot(
            shadow=self.shadow,
            task_store=self._store_tasks,
            event_store=self._store_events,
            runtime_session_store=self._store_runtime_sessions,
            context_block_store=self._context_block_store,
            fabric=self._fabric,
            workflow_store=self._workflow_store,
            world_state_store=self._world_state_store,
            world_state_provider=(
                self._world_state_factory if self.service is None else self.service.world_state
            ),
        )
        self._branch_synthesis = BranchSynthesisMechanism(
            shadow=self.shadow,
            synthesis=self._synthesis,
            dispatcher=self._dispatcher,
            fabric=self._fabric,
            verification_environment=self._verification_environment,
            verification_environment_resolver=self._verification_environment_resolver,
            synthesis_ref=self._synthesis_ref,
        )
        self._invariant_probes = InvariantProbeMechanism(
            dispatcher=self._dispatcher,
            world_state_store=self._world_state_store,
        )

    async def _resolve_task(self, task_id: str):
        store = self._store_tasks
        if task_id and store:
            try:
                row = await store.get(task_id)
                if row:
                    from athena.kernel.lifecycle import deserialize_task

                    return deserialize_task(dict(row))
            except Exception as exc:  # noqa: BLE001
                _logger.warning("task lookup for %s failed: %s", task_id, exc)
        return None

    def _resolve_world_state(self, task_id: str):
        if self._world_state_factory is not None:
            return self._world_state_factory(task_id)
        if self.service is not None:
            return self.service.world_state(task_id)
        raise RuntimeError("FusionOrchestrator has no world-state provider")

    def _fallback_workspace(self, _task_id: str | None = None):
        return self.__dict__.get("_configured_default_workspace")

    async def _task_for(self, task_id: str):
        """Resolve a TaskSpec through the explicit task-store boundary."""
        return await self._task_resolver(task_id)

    def _workspace_for_async(self, task_id: str):
        """Legacy workspace resolution for the service path."""
        return self._workspace_for(task_id)

    def _world_state(self, task_id: str):
        """Resolve world state through the explicit provider boundary."""
        return self._world_state_provider(task_id)

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
            else self._fallback_workspace(task_id)
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
            else self.ports.reality_coordinator
            if self.ports is not None
            else self._reality_coordinator
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
            gate = self.ports.reality_gate if self.ports is not None else self._reality_gate
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
            invariant_set = self._invariant_probes.build(
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
        budget_tracker = self._budgets
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
        parallel: bool = False,
        max_parallel: int = 2,
    ) -> dict[str, Any]:
        """Run bounded alternatives; retained proof feeds exact selection."""
        return await ComparisonLifecycle(self).compare(
            task_id=task_id,
            proposals=proposals,
            invariants=invariants,
            profile=profile,
            parallel=parallel,
            max_parallel=max_parallel,
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
        """Delegate descriptive checkpoint evidence to its projection owner."""
        return await self._semantic_state.capture(
            task_id=task_id,
            workspace_root=workspace_root,
        )

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
        branch_id: str | None = None,
        workspace: Any | None = None,
    ) -> dict:
        """Delegate branch-bound synthesis to the explicit admission mechanism."""
        return await self._branch_synthesis.run(
            registry,
            name=name,
            description=description,
            code=code,
            input_schema=input_schema,
            effects=effects,
            task_id=task_id,
            validation_cases=validation_cases,
            branch_id=branch_id,
            workspace=workspace,
        )

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

    async def _workspace_for(self, task_id: str):
        task = await self._task_for(task_id)
        if task is not None and task.workspace is not None:
            return task.workspace
        workspace = self._fallback_workspace(task_id)
        if workspace is not None:
            return workspace
        raise RuntimeError("FusionOrchestrator has no default workspace configured")

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
