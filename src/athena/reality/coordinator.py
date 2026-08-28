"""Completion authority for speculative / transactional reality.

The dynamic RealityGate lets a coding task accumulate candidate edits in a
shadow workspace, but the ordinary completion path has no step that binds the
task's acceptance criteria to that candidate, proves it, and promotes exactly
the proven revision to reality.  Without that step a speculative task can
pass verification, finish COMPLETE, and its candidate edits never become the
real project.

`RealityCoordinator` is that step.  It is injected into `AgentKernel` and
intercepts every terminal decision *before* `TaskManager.finalize()` persists a
terminal status, so the task can never durably become COMPLETE while its
candidate is unproven or its promotion failed.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, Protocol

from athena.kernel.termination import TerminationDecision
from athena.protocol.events import EV, make_event
from athena.protocol.tasks import (
    Criterion,
    MutationMode,
    TaskSpec,
    TaskStatus,
    WorkspaceSpec,
)
from athena.reality.gate import ExecutionDisposition
from athena.reality.completion import CompletionJournal
from athena.verification import VerificationPlanner

__all__ = [
    "CandidateVerifier",
    "RealityCompletionResult",
    "RealityCoordinator",
    "ShadowCandidateVerifier",
]

_logger = logging.getLogger("athena.reality.coordinator")


class RealityCompletionResult:
    """Outcome of preparing a speculative task for finalization.

    ``decision`` is the (possibly modified) termination decision the kernel
    should finalize with.  ``committed`` is True when a candidate branch was
    promoted to the real workspace during preparation.
    """

    def __init__(
        self,
        *,
        decision: TerminationDecision,
        committed: bool = False,
        branch_id: str | None = None,
        certificate: dict | None = None,
        error: str | None = None,
    ) -> None:
        self.decision = decision
        self.committed = committed
        self.branch_id = branch_id
        self.certificate = certificate
        self.error = error

    @property
    def status(self) -> TaskStatus | None:
        return self.decision.status


class CandidateVerifier(Protocol):
    """Verify acceptance criteria against an explicit workspace."""

    async def verify_against(
        self,
        task: TaskSpec,
        criteria: tuple[Criterion, ...],
        workspace: WorkspaceSpec,
    ) -> list[dict]:
        """Return one dict ``{"id", "passed"}`` per criterion evaluated."""
        ...


class ShadowCandidateVerifier:
    """Routes verification against a candidate shadow workspace.

    Builds a *view* of the task whose workspace is the candidate shadow, then
    reuses the same `CompositeVerifier` the kernel uses at turn boundary.
    Command criteria therefore flow through the dispatcher (which, via the
    active branch, observes the candidate) and file criteria inspect the
    candidate tree directly.
    """

    def __init__(self, verifier: Any) -> None:
        self._verifier = verifier

    async def verify_against(
        self,
        task: TaskSpec,
        criteria: tuple[Criterion, ...],
        workspace: WorkspaceSpec,
    ) -> list[dict]:
        candidate_task = replace(task, workspace=workspace)
        try:
            results = await self._verifier.verify(candidate_task, criteria)
        except Exception as exc:  # noqa: BLE001 - never let verification crash completion
            _logger.warning("candidate verification failed: %s", exc)
            return [{"id": c.id, "passed": False} for c in criteria]
        return [
            {"id": criterion.id, "passed": bool(ok)} for criterion, ok in zip(criteria, results)
        ]


class RealityCoordinator:
    """Bind acceptance evidence to a candidate and promote only proven reality."""

    def __init__(
        self,
        *,
        shadow_engine: Any,
        reality_gate: Any,
        candidate_verifier: CandidateVerifier,
        default_criteria_source: Any = None,
        event_sink: Any = None,
        completion_journal: CompletionJournal | None = None,
        verification_planner: VerificationPlanner | None = None,
        project_index_provider: Any = None,
    ) -> None:
        self._shadow = shadow_engine
        self._gate = reality_gate
        self._verifier = candidate_verifier
        self._default_source = default_criteria_source
        self._planner = verification_planner or VerificationPlanner()
        self._project_index_provider = project_index_provider
        self._plans: dict[str, dict[str, Any]] = {}
        self._event_sink = event_sink
        state_root = getattr(shadow_engine, "_state_root", None)
        self._completion = completion_journal or CompletionJournal(
            state_root or "/tmp/athena-reality"
        )

    async def _emit(self, event_type: str, payload: dict[str, Any], task: TaskSpec) -> None:
        if self._event_sink is None:
            return
        await self._event_sink(
            make_event(
                event_type,
                payload,
                task_id=task.id,
                session_id=task.session_id,
            )
        )

    async def prepare_completion(
        self,
        task: TaskSpec,
        decision: TerminationDecision,
    ) -> RealityCompletionResult:
        """Intercept a terminal decision and resolve any active candidate.

        Only COMPLETE decisions for tasks with an active speculative branch are
        intercepted.  Everything else passes through unchanged.
        """
        if decision.status is not TaskStatus.COMPLETE:
            return RealityCompletionResult(decision=decision)

        branch = self._gate.active_branch(task.id)
        if branch is not None:
            return await self._prepare_speculative_completion(task, decision, branch)

        # Transactional work is already in the real workspace, but it is not
        # accepted merely because the process reached COMPLETE. Verify the
        # current candidate before releasing its compensation checkpoint.
        if self._gate.checkpoint_id(task.id) is not None:
            return await self._prepare_transactional_completion(task, decision)

        return RealityCompletionResult(decision=decision)

    async def _prepare_speculative_completion(
        self,
        task: TaskSpec,
        decision: TerminationDecision,
        branch: Any,
    ) -> RealityCompletionResult:
        """Verify and promote one durable task-local candidate."""

        changed_resources: tuple[str, ...] = ()
        impact: dict[str, Any] = {}
        try:
            diff = getattr(self._shadow, "_diff_trees_async", None)
            changes = await diff(branch) if callable(diff) else {}
            changed_resources = tuple(
                sorted(
                    set(changes.get("modified", ()))
                    | set(changes.get("added", ()))
                    | set(changes.get("deleted", ()))
                )
            )
            impact = await self._impact_for(branch.base_workspace.root, changed_resources)
        except (OSError, RuntimeError, TypeError, ValueError, AttributeError) as exc:
            _logger.warning("candidate impact analysis failed: %s", exc)

        criteria = await self._criteria_for(
            task,
            workspace=branch.shadow_workspace,
            changed_resources=changed_resources,
            impact=impact,
        )

        # Detach the branch from the gate's routing map up front.  This lets
        # verification dispatch directly against the shadow workspace (DIRECT)
        # without the gate rejecting the workspace mismatch, and ensures the
        # subsequent commit routes to the real base workspace.
        await self._gate.deactivate_branch(task.id)

        # The policy profile is part of the environment proof. Bind it before
        # recording verification so the certificate and the later commit use
        # exactly the same policy identity.
        if branch.policy_profile is None and task.metadata:
            branch.policy_profile = task.metadata.get("autonomy")

        await self._emit(
            EV["VERIFICATION_STARTED"],
            {
                "branch_id": branch.id,
                "criteria": [criterion.id for criterion in criteria],
                "workspace": branch.shadow_workspace.root,
            },
            task,
        )

        results = await self._verify_or_fail(task, criteria, branch.shadow_workspace)
        for index, result in enumerate(results, start=1):
            await self._emit(
                EV["VERIFICATION_CHECK_COMPLETED"],
                {
                    "branch_id": branch.id,
                    "criterion": result.get("id"),
                    "status": "passed" if result.get("passed") else "failed",
                    "passed": bool(result.get("passed")),
                },
                task,
            )
            await self._emit(
                EV["CAPABILITY_PROGRESS"],
                {
                    "capability_id": "verification",
                    "value": index,
                    "total": len(results),
                    "unit": "checks",
                    "determinate": True,
                    "message": f"verification checks {index}/{len(results)}",
                },
                task,
            )
        await self._emit(
            EV["VERIFICATION_COMPLETED"],
            {
                "branch_id": branch.id,
                "status": "passed" if all(item.get("passed") for item in results) else "failed",
                "passed": all(item.get("passed") for item in results),
            },
            task,
        )
        await self._shadow.record_verification(
            branch,
            results,
            verification_plan=self._plans.get(task.id),
        )

        if branch.status != "VERIFIED":
            # Known unsupported resource types are rejected before commit and
            # remain retained for an explicit future implementation/discard.
            if not branch.unsupported_resources:
                await self._discard_branch(
                    branch, reason="candidate did not satisfy acceptance criteria"
                )
            return RealityCompletionResult(
                decision=TerminationDecision(
                    terminal=True,
                    status=TaskStatus.PARTIAL,
                    reason=branch.error or "candidate cannot be represented safely",
                ),
                branch_id=branch.id,
            )

        unresolved: list[str] = [str(r["id"]) for r in results if not r.get("passed")]
        if unresolved:
            await self._discard_branch(
                branch, reason="candidate did not satisfy acceptance criteria"
            )
            return RealityCompletionResult(
                decision=TerminationDecision(
                    terminal=True,
                    status=TaskStatus.PARTIAL,
                    reason="acceptance criteria not satisfied against candidate",
                    unresolved=tuple(unresolved),
                ),
                branch_id=branch.id,
            )

        self._completion.begin_verified(
            task_id=task.id,
            branch_id=branch.id,
            decision=decision,
            certificate=branch.verification_certificate,
        )

        commit = await self._shadow.commit(branch, defer_cleanup=True)
        if commit.get("status") == "committed":
            self._completion.mark_commit_proven(
                task.id,
                final_fingerprint=commit.get("final_fingerprint"),
            )
            return RealityCompletionResult(
                decision=decision,
                committed=True,
                branch_id=branch.id,
                certificate=branch.verification_certificate or None,
            )

        error = commit.get("error") or "candidate commit failed"
        status = {
            "CONFLICT": TaskStatus.RECOVERY_REQUIRED,
            "RECOVERY_REQUIRED": TaskStatus.RECOVERY_REQUIRED,
            "STALE_CERTIFICATE": TaskStatus.RECOVERY_REQUIRED,
            "FAILED": TaskStatus.PARTIAL,
        }.get(str(commit.get("status")), TaskStatus.PARTIAL)
        if status is TaskStatus.PARTIAL:
            # No real mutation is in flight for an ordinary failed commit and
            # the shadow cleanup has already completed.  Close the journal so
            # restart recovery does not treat a safely aborted candidate as a
            # proven completion.
            self._completion.mark_aborted(task.id, reason=error)
        else:
            self._completion.mark_recovery_required(
                task.id,
                error=error,
                unresolved=tuple(
                    str(item.get("resource"))
                    for item in commit.get("conflicts", ())
                    if isinstance(item, dict) and item.get("resource")
                ),
            )
        return RealityCompletionResult(
            decision=TerminationDecision(
                terminal=True,
                status=status,
                reason=error,
            ),
            branch_id=branch.id,
            error=error,
        )

    async def _prepare_transactional_completion(
        self,
        task: TaskSpec,
        decision: TerminationDecision,
    ) -> RealityCompletionResult:
        """Verify an in-place candidate in an isolated verification clone."""
        resources = self._transaction_resources(task.id, task.workspace)
        impact = await self._impact_for(
            task.workspace.root if task.workspace else "",
            resources,
        )
        criteria = await self._criteria_for(
            task,
            changed_resources=resources,
            impact=impact,
        )
        await self._emit(
            EV["VERIFICATION_STARTED"],
            {
                "criteria": [criterion.id for criterion in criteria],
                "workspace": task.workspace.root if task.workspace else None,
            },
            task,
        )
        workspace = task.workspace
        verified_owned_fingerprint: str | None = None
        if workspace is not None and criteria:
            checkpoints = getattr(self._gate, "_checkpoints", None)
            owned_fingerprint = getattr(self._gate, "transaction_fingerprint", None)
            owned = owned_fingerprint(task.id) if callable(owned_fingerprint) else None
            if checkpoints is None or not owned:
                return await self._transaction_recovery_result(
                    task,
                    "transaction has no exact owned fingerprint for verification",
                )
            try:
                current_before = await checkpoints.fingerprint(workspace.root)
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                return await self._transaction_recovery_result(
                    task,
                    f"transaction reality could not be fingerprinted before verification: {exc}",
                )
            if current_before != owned:
                return await self._transaction_recovery_result(
                    task,
                    "transaction reality changed before verification could begin",
                )
            # The verification clone is required to represent this exact
            # revision.  The second CAS check below ensures it is still the
            # revision in reality when verification finishes.
            verified_owned_fingerprint = owned
        if workspace is None:
            results = [{"id": "workspace", "passed": False}]
        elif not criteria:
            results = [{"id": "acceptance_criteria", "passed": False}]
        else:
            direct = replace(workspace, mutation_mode=MutationMode.DIRECT)
            verification_branch = await self._shadow.open_branch(
                task_id=task.id,
                base_workspace=direct,
                proposal=[],
                profile=(task.metadata or {}).get("autonomy"),
            )
            try:
                results = await self._verify_or_fail(
                    task,
                    criteria,
                    verification_branch.shadow_workspace,
                )
            finally:
                await self._discard_branch(
                    verification_branch,
                    reason="transactional verification clone discarded",
                )

        for index, result in enumerate(results, start=1):
            await self._emit(
                EV["VERIFICATION_CHECK_COMPLETED"],
                {
                    "criterion": result.get("id"),
                    "status": "passed" if result.get("passed") else "failed",
                    "passed": bool(result.get("passed")),
                },
                task,
            )
            await self._emit(
                EV["CAPABILITY_PROGRESS"],
                {
                    "capability_id": "verification",
                    "value": index,
                    "total": len(results),
                    "unit": "checks",
                    "determinate": True,
                    "message": f"verification checks {index}/{len(results)}",
                },
                task,
            )
        await self._emit(
            EV["VERIFICATION_COMPLETED"],
            {
                "status": "passed" if all(item.get("passed") for item in results) else "failed",
                "passed": all(item.get("passed") for item in results),
            },
            task,
        )

        unresolved: list[str] = [str(r["id"]) for r in results if not r.get("passed")]
        if unresolved:
            try:
                await self._gate.compensate(task.id)
            except Exception as exc:  # noqa: BLE001 - reality must fail closed
                _logger.warning("transactional compensation failed for %s: %s", task.id, exc)
                return RealityCompletionResult(
                    decision=TerminationDecision(
                        terminal=True,
                        status=TaskStatus.RECOVERY_REQUIRED,
                        reason=f"transactional candidate unverified; compensation failed: {exc}",
                        unresolved=tuple(unresolved),
                    ),
                    error=str(exc),
                )
            return RealityCompletionResult(
                decision=TerminationDecision(
                    terminal=True,
                    status=TaskStatus.PARTIAL,
                    reason="acceptance criteria not satisfied against transactional candidate",
                    unresolved=tuple(unresolved),
                ),
            )

        self._completion.begin_verified(
            task_id=task.id,
            branch_id=f"transaction:{task.id}",
            decision=decision,
            certificate={
                "task_id": task.id,
                "workspace": task.workspace.root if task.workspace else None,
            },
        )
        if workspace is None or verified_owned_fingerprint is None:
            return await self._transaction_recovery_result(
                task,
                "transaction verification completed without an exact owned fingerprint",
            )
        checkpoints = getattr(self._gate, "_checkpoints", None)
        if checkpoints is None:
            return await self._transaction_recovery_result(
                task, "transaction checkpoint backend disappeared during verification"
            )
        try:
            current_after = await checkpoints.fingerprint(workspace.root)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return await self._transaction_recovery_result(
                task, f"transaction reality could not be fingerprinted after verification: {exc}"
            )
        if current_after != verified_owned_fingerprint:
            return await self._transaction_recovery_result(
                task,
                "transaction reality changed while its verification clone was running",
            )
        final_fingerprint = verified_owned_fingerprint
        # Persist COMMIT_PROVEN while the compensation checkpoint is still
        # retained.  If the process stops after this write, startup can finish
        # the task without needing the model; if fingerprinting or journaling
        # fails, the recovery binding remains available to an operator.
        self._completion.mark_commit_proven(
            task.id,
            final_fingerprint=final_fingerprint,
        )
        mark_proven = getattr(self._gate, "mark_transaction_proven", None)
        if mark_proven is not None:
            mark_proven(task.id)
        return RealityCompletionResult(decision=decision, committed=True)

    async def _transaction_recovery_result(
        self,
        task: TaskSpec,
        error: str,
    ) -> RealityCompletionResult:
        """Retain the transaction and refuse completion when proof is absent."""
        marker = getattr(self._gate, "mark_transaction_recovery_required", None)
        if callable(marker):
            marker(task.id, error=error)
        self._completion.mark_recovery_required(task.id, error=error)
        return RealityCompletionResult(
            decision=TerminationDecision(
                terminal=True,
                status=TaskStatus.RECOVERY_REQUIRED,
                reason=error,
            ),
            error=error,
        )

    async def mark_finalized(self, task_id: str) -> None:
        """Close the saga after TaskManager durably stores the terminal result."""
        record = self._completion.record(task_id)
        if record is not None and record.get("state") == "COMMIT_PROVEN":
            branch_id = str(record.get("branch_id") or "")
            if branch_id.startswith("transaction:"):
                # Keep the transaction checkpoint alive through the task row's
                # durable terminal write.  A crash between COMMIT_PROVEN and
                # this method must still have compensation material available.
                await self._gate.finalize_transaction(task_id)
            else:
                branch = self._shadow.get_branch(branch_id)
                if branch is not None:
                    # The candidate is evidence for the completion saga.  Do
                    # not garbage-collect it until the terminal task result is
                    # durable; startup reconciliation can repeat this cleanup.
                    await self._shadow.finalize_proven(branch)
        self._completion.mark_finalized(task_id)

    async def reconcile_startup(self, task_manager: Any) -> int:
        """Finish proven completions left between commit and task finalization."""
        recovered = 0
        for record in self._completion.pending():
            task_id = record.get("task_id")
            if not isinstance(task_id, str):
                continue
            task = await task_manager.get(task_id)
            if task is None:
                continue
            status = str((task.metadata or {}).get("status") or "")
            branch = self._shadow.get_branch(str(record.get("branch_id") or ""))
            if record.get("state") == "VERIFIED" and branch is not None:
                if branch.status == "COMMITTED" and branch.commit_state in {
                    "COMMIT_PROVEN",
                    "FINALIZED",
                }:
                    self._completion.mark_commit_proven(
                        task_id,
                        final_fingerprint=branch.commit_outcome.get("final_fingerprint"),
                    )
                    record = self._completion.record(task_id) or record
                else:
                    continue
            if record.get("state") == "RECOVERY_REQUIRED":
                # This is intentionally not auto-resolved.  The branch,
                # certificate, commit plan, and mutation ids remain available
                # for an operator or a dedicated recovery action.
                continue
            if record.get("state") != "COMMIT_PROVEN":
                continue
            if status != TaskStatus.COMPLETE.value and status not in {
                TaskStatus.RUNNING.value,
                TaskStatus.INTERRUPTED.value,
                TaskStatus.RECOVERY_REQUIRED.value,
            }:
                continue
            final_fingerprint = record.get("final_fingerprint")
            current_fingerprint = await self._current_fingerprint(
                task,
                branch,
                branch_id=str(record.get("branch_id") or ""),
            )
            error: str | None = None
            if not final_fingerprint:
                error = "proven completion has no final reality fingerprint"
            elif current_fingerprint is None:
                error = "proven completion reality could not be fingerprinted"
            elif current_fingerprint != final_fingerprint:
                error = "proven completion workspace drifted before task finalization"
            if error is not None:
                self._completion.mark_recovery_required(
                    task_id,
                    error=error,
                )
                if str(record.get("branch_id") or "").startswith("transaction:"):
                    marker = getattr(self._gate, "mark_transaction_recovery_required", None)
                    if callable(marker):
                        marker(task_id, error=error)
                if branch is not None:
                    branch.status = "RECOVERY_REQUIRED"
                    branch.commit_state = "RECOVERY_REQUIRED"
                    branch.error = error
                    self._shadow._persist_branches()
                continue
            if status != TaskStatus.COMPLETE.value:
                await task_manager.finalize(
                    task_id,
                    status=TaskStatus.COMPLETE,
                    reason=str(record.get("reason") or "recovered proven completion"),
                    summary=str(record.get("summary") or "recovered proven completion"),
                    _allow_recovery_completion=True,
                )
            if branch is not None:
                await self._shadow.finalize_proven(branch)
            elif str(record.get("branch_id") or "").startswith("transaction:"):
                # A transactional commit has no shadow branch to finalize.
                # Release its checkpoint only after COMMIT_PROVEN is durable;
                # if this cleanup is interrupted, the journal remains pending
                # and startup retries it without asking the model.
                await self._gate.finalize_transaction(task_id)
            self._completion.mark_finalized(task_id)
            recovered += 1
        return recovered

    async def _current_fingerprint(
        self,
        task: TaskSpec,
        branch: Any | None,
        *,
        branch_id: str = "",
    ) -> str | None:
        """Read current reality for the final completion-saga CAS check."""
        root = None
        if branch is not None:
            root = getattr(getattr(branch, "base_workspace", None), "root", None)
        if root is None:
            root = getattr(getattr(task, "workspace", None), "root", None)
        if root is None:
            return None
        # Transactional fingerprints use the checkpoint backend's canonical
        # structured manifest, while shadow commit fingerprints use the
        # shadow engine's compact manifest. Match the producer that issued
        # the journal record rather than comparing unlike representations.
        actual_branch_id = getattr(branch, "id", "") if branch is not None else branch_id
        if actual_branch_id.startswith("transaction:"):
            checkpoint_manager = getattr(self._gate, "_checkpoints", None)
            fingerprint = getattr(checkpoint_manager, "fingerprint", None)
            if fingerprint is not None:
                try:
                    return await fingerprint(root)
                except (OSError, RuntimeError, TypeError, ValueError):
                    return None
        fingerprint = getattr(self._shadow, "workspace_fingerprint", None)
        if fingerprint is None:
            return None
        try:
            return await fingerprint(root)
        except (OSError, RuntimeError, TypeError, ValueError):
            return None

    async def _criteria_for(
        self,
        task: TaskSpec,
        *,
        workspace: WorkspaceSpec | None = None,
        changed_resources: tuple[str, ...] = (),
        impact: Mapping[str, Any] | None = None,
    ) -> tuple[Criterion, ...]:
        explicit = tuple(c for c in task.acceptance_criteria if c.required)
        baseline = tuple(
            await self._derive_default_criteria(
                task,
                workspace=workspace,
                changed_resources=changed_resources,
                impact=impact,
            )
        )
        if not explicit:
            return baseline

        # Explicit criteria are semantic requirements supplied by the task;
        # they strengthen the project-derived proof instead of replacing it.
        criteria = _deduplicate_criteria((*explicit, *baseline))
        plan = dict(self._plans.get(task.id) or {})
        plan.update(
            {
                "plan_id": plan.get("plan_id") or f"explicit:{task.id}",
                "impacted_tests": plan.get("impacted_tests") or list(_impact_tests(impact)),
                "invariants": plan.get("invariants")
                or list((task.metadata or {}).get("invariants") or ()),
                "required_strength": plan.get("required_strength") or "standard",
                "rationale": list(plan.get("rationale") or ())
                + ["task supplied explicit acceptance criteria; baseline retained"],
                "index_revision": plan.get("index_revision")
                or (impact or {}).get("index_revision"),
                "explicit_criteria": [criterion.id for criterion in explicit],
            }
        )
        self._plans[task.id] = plan
        return criteria

    async def _verify_or_fail(
        self,
        task: TaskSpec,
        criteria: tuple[Criterion, ...],
        workspace: WorkspaceSpec,
    ) -> list[dict]:
        if not criteria:
            return [{"id": "acceptance_criteria", "passed": False}]
        try:
            results = await self._verifier.verify_against(task, criteria, workspace)
        except Exception as exc:  # noqa: BLE001 - never accept unverified work
            _logger.warning("candidate verification failed: %s", exc)
            return [{"id": c.id, "passed": False} for c in criteria]
        normalized = list(results or ())
        if len(normalized) != len(criteria):
            return [{"id": c.id, "passed": False} for c in criteria]
        return normalized

    async def _discard_branch(self, branch: Any, *, reason: str) -> None:
        try:
            await self._shadow.discard(branch, reason=reason)
        except Exception as exc:  # noqa: BLE001 - retain recovery visibility
            _logger.warning("discard branch %s failed: %s", branch.id, exc)

    async def discard_incomplete(
        self,
        task_id: str,
        terminal_status: TaskStatus,
    ) -> TaskStatus | None:
        """Discard any active candidate when a task ends without completing.

        A cancelled / failed / partial task must not leave a speculative branch
        claiming to be that task's candidate.
        """
        branch = self._gate.active_branch(task_id)
        if branch is not None:
            try:
                await self._shadow.discard(branch, reason=f"task {task_id} {terminal_status.value}")
                await self._gate.deactivate_branch(task_id)
            except Exception as exc:  # noqa: BLE001
                _logger.warning("discard incomplete branch %s failed: %s", branch.id, exc)
                return TaskStatus.RECOVERY_REQUIRED

        if self._gate.checkpoint_id(task_id) is not None:
            try:
                await self._gate.compensate(task_id)
            except Exception as exc:  # noqa: BLE001 - do not hide live mutations
                _logger.warning("transactional compensation for %s failed: %s", task_id, exc)
                return TaskStatus.RECOVERY_REQUIRED
        return None

    async def _derive_default_criteria(
        self,
        task: TaskSpec,
        *,
        workspace: WorkspaceSpec | None = None,
        changed_resources: tuple[str, ...] = (),
        impact: Mapping[str, Any] | None = None,
    ) -> list[Criterion]:
        """Derive a bounded verification plan when the user set no criteria.

        Uses the project profile's configured commands so "the model said it
        looks done" is never the only proof that lets a candidate cross into
        reality.
        """
        if self._default_source is None:
            return []
        try:
            profile_task = replace(task, workspace=workspace) if workspace is not None else task
            profile = self._default_source(profile_task)
            if hasattr(profile, "__await__"):
                profile = await profile
        except Exception as exc:  # noqa: BLE001
            _logger.warning("default criteria derivation failed: %s", exc)
            return []
        if not profile:
            return []
        plan = self._planner.plan(
            task,
            profile,
            changed_resources=changed_resources,
            impact=impact,
            invariants=tuple((task.metadata or {}).get("invariants") or ()),
        )
        self._plans[task.id] = {
            "plan_id": plan.plan_id,
            "impacted_resources": list(plan.impacted_resources),
            "impacted_tests": list(plan.impacted_tests),
            "invariants": list(plan.invariants),
            "required_strength": plan.required_strength,
            "rationale": list(plan.rationale),
            "index_revision": plan.index_revision,
        }
        if plan.skipped_commands:
            _logger.info(
                "verification planner skipped %d unusable project probes",
                len(plan.skipped_commands),
            )
        return list(plan.criteria)

    async def _impact_for(
        self,
        root: str,
        changed_resources: tuple[str, ...],
    ) -> dict[str, Any]:
        """Use the persisted project-index graph when deriving completion proof."""
        if not root or not changed_resources or self._project_index_provider is None:
            return {}
        try:
            index = self._project_index_provider(root)
            if hasattr(index, "__await__"):
                index = await index
            impact = index.impact(list(changed_resources))
            return dict(impact) if isinstance(impact, Mapping) else {}
        except (OSError, RuntimeError, TypeError, ValueError, AttributeError) as exc:
            _logger.warning("project index impact lookup failed: %s", exc)
            return {}

    def _transaction_resources(
        self,
        task_id: str,
        workspace: WorkspaceSpec | None,
    ) -> tuple[str, ...]:
        records = getattr(self._gate, "_transaction_records", {})
        raw = records.get(task_id, {}).get("resources", [])
        root = workspace.root if workspace is not None else ""
        values: list[str] = []
        for resource in raw:
            path = str(resource)
            if root and path.startswith(root + "/"):
                path = path[len(root) + 1 :]
            values.append(path)
        return tuple(sorted(set(values)))


def disposition_for_metadata(metadata: dict | None) -> ExecutionDisposition | None:
    """Extract the resolved disposition from a capability result's reality metadata."""
    if not metadata:
        return None
    reality = metadata.get("reality") or {}
    value = reality.get("disposition")
    if value is None:
        return None
    try:
        return ExecutionDisposition(str(value))
    except ValueError:
        return None


def _impact_tests(impact: Mapping[str, Any] | None) -> tuple[str, ...]:
    """Extract concrete test resources from a project-index impact record."""
    if not isinstance(impact, Mapping):
        return ()
    values = impact.get("affected_tests") or ()
    if not isinstance(values, (list, tuple, set)):
        return ()
    return tuple(sorted({str(value) for value in values if str(value).strip()}))


def _deduplicate_criteria(criteria: tuple[Criterion, ...]) -> tuple[Criterion, ...]:
    """Keep one proof for equivalent checks, preferring explicit criteria."""
    seen: set[tuple[Any, ...]] = set()
    output: list[Criterion] = []
    for criterion in criteria:
        verification = criterion.verification
        key: tuple[Any, ...]
        if verification is None:
            key = ("criterion", criterion.id)
        else:
            key = (
                "verification",
                verification.type.value,
                verification.command,
                verification.path,
                verification.predicate,
                verification.capability,
            )
        if key in seen:
            continue
        seen.add(key)
        output.append(criterion)
    return tuple(output)
