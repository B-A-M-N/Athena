"""Operator interaction: cancel/interrupt, input, approvals, resume (P1-10).

Mechanism, not a second authority. Cancelling or interrupting a task,
answering an operator-input request, granting or denying an approval
(installing scoped grants, rehydrating them across restart), and
resuming interrupted work are state transitions the kernel and task
manager already define. Every seam — the task manager, kernel, stores,
policy engine, cancellation manager, and submission — resolves through
the :class:`AthenaService` instance this object is constructed with, and
the service keeps delegate methods, so the public API, event order, and
instance-attribute patching are unchanged from when these bodies lived
on the facade.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from athena.kernel.continuations import ContinuationStore
from athena.protocol.policy import ApprovalScope, Principal
from athena.protocol.tasks import AgentRequest, TaskSpec, TaskStatus
from athena.state.approvals import ApprovalStore

if TYPE_CHECKING:
    from athena.service.service import AthenaService

_logger = logging.getLogger("athena.service")

__all__ = ["OperatorInteractionService"]


class OperatorInteractionService:
    """Operator-facing cancel/input/approval/resume mechanism owned by the facade."""

    def __init__(self, service: AthenaService) -> None:
        self._svc = service

    async def cancel(self, task_id: str, reason: str = "cancelled by user") -> TaskStatus:
        status = await self._svc._require_cancellations().cancel(task_id, reason)
        if self._svc._kernel is not None:
            try:
                self._svc._kernel.cancel_task(task_id)
            except Exception as exc:
                # P1-11: a failed kernel cancel can leave work running after
                # the operator's cancel call returned success.
                _logger.warning(
                    "kernel cancel_task failed for %s: %s: %s", task_id, type(exc).__name__, exc
                )
            try:
                await self._svc._kernel.notify_approval_resolved(task_id, "denied")
            except Exception as exc:
                _logger.warning(
                    "approval denial notification failed for %s: %s: %s",
                    task_id,
                    type(exc).__name__,
                    exc,
                )
        return status

    async def interrupt(self, task_id: str, reason: str = "externally interrupted") -> TaskStatus:
        return await self._svc._require_cancellations().interrupt(task_id, reason)

    async def pending_input(self, task_id: str) -> dict | None:
        """The open clarification request for a task, if any."""
        if self._svc._store_input_requests is None:
            return None
        return await self._svc._store_input_requests.pending_for_task(task_id)

    async def provide_input(self, task_id: str, answer: str) -> None:
        """Answer a task's open clarification and resume the SAME task.

        The question was persisted when the model issued ``request_input``;
        this records the answer durably and wakes the parked kernel loop, which
        continues the identical Task with the answer in its session.

        With worker slot release (P1-17) the parked task may have no live
        coroutine: the run returned when the slot deadline fired. The answer
        is durable either way (ANSWERED_PENDING_RESUME), so the no-live-run
        branch relaunches the task exactly like the approval path does — the
        relaunched loop consumes the durable answer before its first model
        call.
        """
        if self._svc._store_input_requests is None:
            raise RuntimeError("operator input is unavailable in this deployment")
        request = await self._svc._store_input_requests.pending_for_task(task_id)
        if request is None:
            raise KeyError(f"No pending input request for task: {task_id}")
        await self._svc._store_input_requests.resolve(request["id"], str(answer))
        kernel = self._svc._kernel
        active = bool(kernel is not None and task_id in getattr(kernel, "_runs", {}))
        if active and kernel is not None:
            await kernel.notify_input_provided(task_id, str(answer))
            return
        if kernel is None or self._svc._task_manager is None:
            return
        row = await self._svc._store_tasks.get(task_id) if self._svc._store_tasks else None
        if row and row.get("status") == TaskStatus.WAITING_INPUT.value:
            await self._svc._task_manager.transition(task_id, TaskStatus.RUNNING)
            relaunch = asyncio.create_task(kernel.run_task(task_id))
            self._svc._approval_recovery_tasks.add(relaunch)
            relaunch.add_done_callback(
                self._svc._log_background_failure(f"input relaunch {task_id}")
            )

    async def approve(self, approval_id: str, *, granted: bool, scope: str | None = None) -> None:
        """Resolve a pending approval and wake the parked task, if any.

        The persisted resolution (ApprovalStore) and the runtime grant
        (ApprovalManager) share the same approval_id. A granted call installs an
        exact scoped ApprovalGrant so the SAME capability call (identical
        arguments) passes policy on resume; a denied call records the denial and
        wakes the task with no effect (BHV-043).
        """
        approvals = self._svc._store_approvals
        if approvals is None:
            raise RuntimeError("approval persistence is unavailable")
        rec = await approvals.get(approval_id)
        if not isinstance(rec, dict):
            raise KeyError(f"Approval not found: {approval_id}")
        if rec.get("status") != ApprovalStore.PENDING:
            raise ValueError(f"Approval already resolved: {approval_id}")
        task_id = rec.get("task_id")
        metadata = rec.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise ValueError(f"Approval metadata is invalid: {approval_id}")

        effective_scope: ApprovalScope | None = None
        if granted:
            effective_scope = self._clamp_approval_scope(scope, metadata)
            if effective_scope is None:
                raise ValueError(f"Unsupported approval scope for {approval_id}")
            if (
                effective_scope is ApprovalScope.CALL
                and not metadata.get("args_digest")
                and not metadata.get("candidate_apply")
            ):
                raise ValueError(f"CALL approval has no argument binding: {approval_id}")

        # This is the authority boundary.  Nothing below may install a grant,
        # resolve a continuation, or wake a task until this durable CAS has
        # committed successfully.  Persistence failure therefore leaves the
        # request parked and observable as PENDING.
        if granted:
            await approvals.record_grant(
                approval_id,
                resolver="user",
                scope=effective_scope.value if effective_scope else None,
                expires_at=metadata.get("expires_at"),
                metadata={"resolved_by_service": True},
            )
        else:
            await approvals.record_deny(
                approval_id, resolver="user", metadata={"resolved_by_service": True}
            )

        # Candidate deletion approvals are operator decisions over a durable
        # ShadowEngine commit plan, not parked kernel capability calls. Apply
        # the retained plan after the approval is persisted and do not wake a
        # normal task continuation for this review-only path.
        if metadata.get("candidate_apply"):
            if granted and task_id is not None:
                try:
                    await self._svc.apply_candidate(task_id, approval_id=approval_id)
                except Exception as exc:  # preserve the candidate for recovery
                    await self._mark_approval_recovery(task_id, approval_id, exc)
                    raise
            return

        # Durable continuation: retain the canonical call until the kernel
        # consumes it. A live kernel wakes its in-memory wait; after restart,
        # no coroutine exists, so transition the same task back to RUNNING and
        # launch the normal kernel entry point, which claims the stored call.
        store_cont = getattr(self._svc, "_store_continuations", None)
        if metadata.get("call_id"):
            try:
                if store_cont is None:
                    raise RuntimeError("durable continuation store is unavailable")
                resolved = False
                for cont in await store_cont.pending(task_id):
                    if cont.get("call_id") == metadata.get("call_id"):
                        await store_cont.mark_resolved(
                            cont["id"], "granted" if granted else "denied"
                        )
                        resolved = True
                        break
                if not resolved:
                    raise LookupError(
                        f"continuation not found for approval {approval_id} "
                        f"and call {metadata.get('call_id')}"
                    )
            except Exception as exc:
                await self._mark_approval_recovery(task_id, approval_id, exc)
                raise

        if granted:
            try:
                self._install_grant(approval_id, task_id, metadata, first=effective_scope)
            except Exception as exc:
                await self._mark_approval_recovery(task_id, approval_id, exc)
                raise

        kernel = self._svc._kernel
        active = bool(
            task_id is not None and kernel is not None and task_id in getattr(kernel, "_runs", {})
        )
        try:
            if active and kernel is not None and task_id is not None:
                await kernel.notify_approval_resolved(task_id, "granted" if granted else "denied")
            elif task_id is not None and kernel is not None and self._svc._task_manager is not None:
                row = await self._svc._store_tasks.get(task_id) if self._svc._store_tasks else None
                if row and row.get("status") == TaskStatus.WAITING_APPROVAL.value:
                    await self._svc._task_manager.transition(task_id, TaskStatus.RUNNING)
                    recovery = asyncio.create_task(kernel.run_task(task_id))
                    recovery.add_done_callback(
                        self._svc._log_background_failure(f"approval recovery {task_id}")
                    )
        except Exception as exc:
            await self._mark_approval_recovery(task_id, approval_id, exc)
            raise

    async def _mark_approval_recovery(
        self, task_id: str | None, approval_id: str, error: BaseException
    ) -> None:
        """Make post-persistence approval uncertainty explicit and durable."""
        if task_id is None or self._svc._task_manager is None:
            _logger.error("approval %s requires recovery: %s", approval_id, error)
            return
        try:
            row = await self._svc._store_tasks.get(task_id) if self._svc._store_tasks else None
            current = row.get("status") if row else None
            if current in {
                TaskStatus.WAITING_APPROVAL.value,
                TaskStatus.RUNNING.value,
                TaskStatus.INTERRUPTED.value,
            }:
                await self._svc._task_manager.transition(
                    task_id,
                    TaskStatus.RECOVERY_REQUIRED,
                    reason=f"approval {approval_id} resolution requires recovery: {error}",
                )
        except Exception as recovery_error:
            # Preserve the original resolution error while making the failed
            # recovery transition visible in logs for an operator.
            _logger.error("approval %s recovery transition failed: %s", approval_id, recovery_error)

    def _install_grant(
        self,
        approval_id: str,
        task_id: str | None,
        metadata: dict,
        scope: str | None = None,
        first: ApprovalScope | None = None,
        expires_at: datetime | None = None,
    ) -> None:
        """Install an exact scoped ApprovalGrant so the approved call passes on resume."""
        if self._svc._policy is None or getattr(self._svc._policy, "approvals", None) is None:
            raise RuntimeError("runtime approval manager is unavailable")
        manager = self._svc._policy.approvals
        digest = metadata.get("args_digest")
        scope_choice = first or self._clamp_approval_scope(scope, metadata)
        if scope_choice is None:
            raise ValueError("approval scope is not offered by the request")
        if scope_choice == ApprovalScope.CALL and not digest:
            raise ValueError("CALL approval requires an exact argument digest")
        cap = metadata.get("capability_id")
        call_id = metadata.get("call_id")
        effects = metadata.get("effects") or []
        primary_name = effects[0] if effects and isinstance(effects, list) else None

        # Exact-args pinning is a TOCTOU guard for resuming THE approved call
        # (CALL scope).  TASK/SESSION/PROJECT scopes authorize future calls and
        # must not be pinned to one argument digest, or they never match.
        pinned_digest = digest or None
        pinned_call = call_id
        if scope_choice != ApprovalScope.CALL:
            pinned_digest = None
            pinned_call = None

        if manager.state(approval_id) is None:
            manager.create_request(
                Principal("agent", "athena"),
                scope_choice,
                capability=cap,
                effect=str(primary_name) if primary_name else None,
                # Authority envelope (P0): persist the COMPLETE effect set
                # from the original request so the grant's ceiling is what
                # the operator approved, never broader.
                allowed_effects=tuple(effects) if effects else None,
                task_id=task_id,
                # SESSION-scoped grants are keyed on session_id in
                # ApprovalManager._covers_locked; omitting it makes every
                # session grant unmatchable and forces re-approval.
                session_id=metadata.get("session_id"),
                approval_id=approval_id,
                args_digest=pinned_digest,
                call_id=pinned_call,
                expires_at=expires_at,
            )
        manager.grant(approval_id, resolver="user")

    async def _rehydrate_approval_grants(
        self,
        approvals: ApprovalStore,
        continuations: ContinuationStore,
    ) -> None:
        """Restore only persisted grants that are still safe to use.

        CALL grants are rehydrated only when their exact durable continuation
        is resolved and unconsumed. Without that check, restarting Athena
        would reset the in-memory ``used`` bit and make a one-shot approval
        replayable. Broader scopes are restored from their persisted grant
        rows and retain the original expiry boundary.
        """
        if self._svc._policy is None:
            return
        try:
            records = await approvals.list_granted()
        except Exception as exc:
            _logger.warning("approval grant rehydration failed: %s", exc)
            return
        for record in records:
            metadata = record.get("metadata") or {}
            if not isinstance(metadata, dict):
                metadata = {}
            scope_raw = record.get("grant_scope") or metadata.get("scope")
            try:
                scope = ApprovalScope(scope_raw)
            except (TypeError, ValueError):
                _logger.warning(
                    "skipping granted approval %s with invalid scope %r",
                    record.get("id"),
                    scope_raw,
                )
                continue

            approval_id = str(record.get("id") or "")
            if scope is ApprovalScope.CALL:
                try:
                    pending = await continuations.unconsumed_for_approval(approval_id)
                except Exception as exc:
                    _logger.warning(
                        "cannot check approval continuation %s: %s",
                        approval_id,
                        exc,
                    )
                    continue
                if not pending:
                    continue

            raw_expiry = record.get("grant_expires_at") or metadata.get("expires_at")
            expiry = None
            if raw_expiry:
                try:
                    expiry = datetime.fromisoformat(str(raw_expiry))
                except ValueError:
                    _logger.warning("ignoring invalid expiry on approval %s", approval_id)
            self._install_grant(
                approval_id,
                record.get("task_id"),
                metadata,
                first=scope,
                expires_at=expiry,
            )

    def _clamp_approval_scope(self, choice: str | None, metadata: dict) -> ApprovalScope | None:
        """Resolve the effective approval scope.

        A caller-provided ``choice`` is clamped to the scopes the approval
        store actually requested (``metadata["requested_scope"]``). Defaults to
        the stored ``scope`` (or the single requested scope) when the caller
        offers none; returns None when the caller requests a scope that was not
        offered, so an unsupported/broader grant is never installed.
        """
        requested = metadata.get("requested_scope")
        supported: set[str] = set()
        if isinstance(requested, list):
            supported = {str(s) for s in requested}
        elif isinstance(requested, str):
            supported = {requested}

        default = metadata.get("scope")
        if default is None and len(supported) == 1:
            default = next(iter(supported))

        if choice in (None, ""):
            if default:
                try:
                    if supported and str(default) not in supported:
                        return None
                    return ApprovalScope(default)
                except (ValueError, KeyError):
                    return None
            return None

        if choice not in supported:
            return None
        try:
            return ApprovalScope(choice)
        except (TypeError, ValueError):
            return None

    async def pending_approval_id(self, task_id: str) -> str | None:
        """Return the id of the most recent pending approval for a task, if any."""
        if self._svc._store_approvals is None:
            return None
        try:
            recs = await self._svc._store_approvals.list_for_task(task_id)
        except Exception:
            return None
        for rec in recs or []:
            if isinstance(rec, dict) and rec.get("status") == "PENDING":
                return rec.get("id") or rec.get("approval_id")
        return None

    async def list_sessions(self) -> list[dict]:
        if self._svc._sessions is None:
            return []
        return await self._svc._sessions.list_all()

    async def resume(self, session_id: str, *, prompt: str = "") -> TaskSpec:
        """Create and run a follow-up task in the given session."""
        return await self._svc.submit(
            AgentRequest(prompt=prompt or "continue", session_id=session_id),
            wait=True,
        )

    async def list_interrupted(self) -> list[dict]:
        """Tasks parked by shutdown/crash, still awaiting completion."""
        if self._svc._store_tasks is None:
            return []
        try:
            rows = await self._svc._store_tasks.list_by_status(TaskStatus.INTERRUPTED)
        except Exception as exc:
            _logger.warning("interrupted task listing failed: %s", exc)
            return []
        out = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            out.append(
                {
                    "id": row.get("id"),
                    "objective": row.get("objective"),
                    "session_id": row.get("session_id"),
                    "created_at": row.get("created_at"),
                }
            )
        return out

    async def resume_task(self, task_id: str) -> TaskSpec:
        """Re-queue an INTERRUPTED task so it runs to completion.

        The task keeps its original objective, acceptance criteria, workspace,
        capability policy, and budget — this is a continuation of the SAME
        durable work, not a new conversation turn.
        """
        if self._svc._store_tasks is None or self._svc._task_manager is None:
            raise RuntimeError("AthenaService not started")
        row = await self._svc._store_tasks.get(task_id)
        if row is None:
            raise KeyError(f"Task not found: {task_id}")
        status = (row.get("status") or "").upper()
        if status == "RUNNING":
            # Already claimed by a live worker.
            return await self._svc.get_task(task_id)
        if status in ("COMPLETE", "FAILED", "CANCELLED"):
            raise ValueError(f"task {task_id} is terminal ({status}); cannot resume")
        # INTERRUPTED (and QUEUED re-queue): hand back to the worker pool.
        await self._svc._task_manager.enqueue(task_id)
        return await self._svc.get_task(task_id)
