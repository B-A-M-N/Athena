"""Durable continuation coordination — extracted from AgentKernel (P1-10).

Mechanism, not a second authority. Resuming an approved capability call,
reconciling workflow parents and child continuations, consuming/releasing
durable claims, and scrubbing input answers are bookkeeping around the one
decision the kernel already made.

Every attribute access — stores, dispatch, AND the cluster's own methods —
resolves through the bound :class:`AgentKernel` instance (``self._k``), so a
delegate call observes exactly the same instance-attribute patches the
method would have observed living on the kernel. The kernel keeps delegate
entrypoints and stays the only caller that decides WHEN this mechanism runs.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import replace
from typing import TYPE_CHECKING

from athena.kernel.dispatch import DispatchResult
from athena.protocol.continuations import SuspendedCall
from athena.protocol.capabilities import (
    CapabilityRequest,
    CapabilityRequestOrigin,
    DispatchDirectives,
)
from athena.protocol.ids import new_id
from athena.protocol.messages import (
    CapabilityResultBlock,
    Message,
    Provenance,
    Role,
    SourceType,
    TextBlock,
    utcnow,
)

from athena.concurrency import ReferenceCountedKeyedLocks
from athena.kernel.termination import TerminationDecision

if TYPE_CHECKING:
    from athena.kernel.kernel import AgentKernel
    from athena.protocol.tasks import TaskResult
    from athena.protocol.tasks import TaskSpec
from athena.protocol.tasks import TaskStatus


from athena.kernel.policy_context import (
    deny_result as _deny_result,
    replay_policy_context as _replay_policy_context,
    remaining_runtime_seconds as _remaining_runtime_seconds,
)


_logger = logging.getLogger("athena.kernel")

__all__ = ["ContinuationCoordinator", "_deny_result_for_request", "_to_result_block"]


class ContinuationCoordinator:
    """Continuation resume/reconcile mechanism owned by AgentKernel."""

    def __init__(self, kernel: AgentKernel) -> None:
        self._k = kernel

    async def _resume_workflow_parent(
        self,
        task: TaskSpec,
        suspended: SuspendedCall | None = None,
        *,
        record: dict | None = None,
        shim=None,
    ):
        """Finish the outer workflow call after one child approval resolves.

        A workflow step is dispatched as its own canonical capability call, so
        the approval belongs to that child.  Without this small bridge the
        kernel would append the child's result but leave the model's original
        ``workflow`` call unresolved.  The resumed workflow run owns the
        continuation and decides whether to execute the next step or return a
        final workflow result.
        """
        directives = getattr(suspended, "directives", None)
        policy_context = dict((record or {}).get("policy_context") or {})
        parent_request = getattr(suspended, "workflow_parent_request", None)
        run_id = (
            getattr(suspended, "workflow_run_id", None)
            or getattr(directives, "workflow_run_id", None)
            or policy_context.get("workflow_run_id")
        )
        workflow_id = (
            getattr(suspended, "workflow_id", None)
            or getattr(directives, "workflow_id", None)
            or policy_context.get("workflow_id")
        )
        parent_call_id = (
            getattr(directives, "workflow_parent_call_id", None)
            or policy_context.get("workflow_parent_call_id")
            or getattr(parent_request, "call_id", None)
        )
        parent_capability_id = (
            getattr(directives, "workflow_parent_capability_id", None)
            or policy_context.get("workflow_parent_capability_id")
            or "workflow"
        )
        if not run_id or not workflow_id or not parent_call_id:
            return None
        shim = self._k._dispatch_factory(task) if self._k._dispatch_factory is not None else None
        if shim is None:
            return CapabilityResultBlock(
                call_id=str(parent_call_id),
                capability_id=str(parent_capability_id),
                ok=False,
                error="workflow approval continuation has no dispatch context",
            )

        if parent_request is None:
            parent_request = CapabilityRequest(
                capability_id=str(parent_capability_id),
                arguments={
                    "operation": "run",
                    "workflow_id": str(workflow_id),
                    "run_id": str(run_id),
                },
                task_id=task.id,
                session_id=task.session_id,
                call_id=str(parent_call_id),
                origin=CapabilityRequestOrigin.TRUSTED_ORCHESTRATION,
            )
        else:
            arguments = dict(parent_request.arguments or {})
            arguments["run_id"] = str(run_id)
            parent_request = replace(
                parent_request,
                arguments=arguments,
                origin=CapabilityRequestOrigin.TRUSTED_ORCHESTRATION,
            )
        try:
            return await shim.resume_request(
                task,
                parent_request,
                **_replay_policy_context(task),
            )
        except Exception as exc:  # broad-exception: the outer result must remain truthful
            return CapabilityResultBlock(
                call_id=parent_request.call_id,
                capability_id=parent_request.capability_id,
                ok=False,
                error=f"workflow approval continuation failed: {exc}",
            )

    async def _reconcile_workflow_suspended(
        self,
        suspended,
        result,
        *,
        workspace_root: str | None = None,
        workspace=None,
    ) -> None:
        """Record same-process approval completion for a durable workflow step."""
        if self._k._workflow_run_store is None:
            return
        directives = getattr(suspended, "directives", None)
        if directives is None or not directives.workflow_run_id:
            return
        status = getattr(result, "status", None)
        status = getattr(status, "value", status)
        ok = status == "ok" if status is not None else bool(getattr(result, "ok", False))
        failures = () if ok else (getattr(result, "error", None) or "approved call failed",)
        completion = {
            "output": getattr(result, "output", None),
            "failures": failures,
            "workspace_root": workspace_root,
        }
        if workspace is not None:
            completion["workspace"] = workspace
        await self._k._workflow_run_store.complete_call(suspended.call_id, **completion)

    async def _mark_continuations_consumed(self, suspended) -> None:
        if self._k._continuation_store is None:
            return
        for item in suspended:
            call_id = getattr(item, "call_id", None)
            if not call_id:
                continue
            try:
                await self._k._continuation_store.mark_consumed_for_call(call_id)
            except Exception as exc:  # broad-exception: continuation boundary converts durable-store/binding failures into truthful task outcomes
                _logger.warning("continuation consume failed for %s: %s", call_id, exc)

    async def _resume_durable_continuation(self, task: TaskSpec) -> SuspendedCall | None:
        if self._k._continuation_store is None or self._k._dispatch_factory is None:
            return None
        try:
            record = await self._k._continuation_store.claim_resolved(task.id)
        except Exception as exc:  # broad-exception: continuation boundary converts durable-store/binding failures into truthful task outcomes
            _logger.warning("durable continuation lookup failed for %s: %s", task.id, exc)
            return None
        if record is None:
            return None

        call_id = str(record.get("call_id") or new_id("call"))
        shim = self._k._dispatch_factory(task)
        policy_context = record.get("policy_context") or {}
        request = CapabilityRequest(
            capability_id=str(record.get("capability_id") or ""),
            arguments=dict(record.get("canonical_arguments") or {}),
            task_id=task.id,
            session_id=task.session_id,
            call_id=call_id,
            # This is already canonical, durable Athena state. Re-running the
            # model compatibility repair here would make replay policy-version
            # dependent and violate the approval TOCTOU binding.
            origin=CapabilityRequestOrigin.TRUSTED_ORCHESTRATION,
        )
        if record.get("decision") not in (None, "granted"):
            denied = _deny_result_for_request(request)
            await self._k._reconcile_workflow_continuation(record, denied)
            parent = await self._k._resume_workflow_parent(
                task,
                record=record,
                shim=shim,
            )
            blocks = [denied]
            if isinstance(parent, SuspendedCall):
                await self._k._append_results(task, blocks)
                await self._k._consume_durable_call(call_id)
                return parent
            if parent is not None:
                blocks.append(_to_result_block(parent))
            await self._k._append_results(task, blocks)
            await self._k._consume_durable_call(call_id)
            return None

        try:
            directives = None
            workflow_run_id = policy_context.get("workflow_run_id")
            if workflow_run_id:
                directives = DispatchDirectives(
                    workflow_run_id=str(workflow_run_id),
                    workflow_step_id=(
                        str(policy_context["workflow_step_id"])
                        if policy_context.get("workflow_step_id") is not None
                        else None
                    ),
                    workflow_item_index=(
                        int(policy_context["workflow_item_index"])
                        if policy_context.get("workflow_item_index") is not None
                        else None
                    ),
                    workflow_execution_id=(
                        str(policy_context["workflow_execution_id"])
                        if policy_context.get("workflow_execution_id") is not None
                        else None
                    ),
                    workflow_parent_call_id=(
                        str(policy_context["workflow_parent_call_id"])
                        if policy_context.get("workflow_parent_call_id") is not None
                        else None
                    ),
                    workflow_parent_capability_id=(
                        str(policy_context["workflow_parent_capability_id"])
                        if policy_context.get("workflow_parent_capability_id") is not None
                        else None
                    ),
                    workflow_id=(
                        str(policy_context["workflow_id"])
                        if policy_context.get("workflow_id") is not None
                        else None
                    ),
                )
            result = await shim.resume_request(
                task,
                request,
                directives=directives,
            )
            if isinstance(result, SuspendedCall):
                await self._k._append_results(
                    task,
                    [
                        CapabilityResultBlock(
                            call_id=call_id,
                            capability_id=request.capability_id,
                            ok=False,
                            error="approval continuation could not be resumed",
                        )
                    ],
                )
                await self._k._release_durable_call(call_id)
            else:
                await self._k._reconcile_workflow_continuation(
                    record,
                    result,
                    workspace_root=shim.scope.workspace.root,
                    workspace=shim.scope.workspace,
                )
                parent = await self._k._resume_workflow_parent(
                    task,
                    record=record,
                    shim=shim,
                )
                blocks = [_to_result_block(result)]
                if isinstance(parent, SuspendedCall):
                    await self._k._append_results(task, blocks)
                    await self._k._consume_durable_call(call_id)
                    return parent
                if parent is not None:
                    blocks.append(_to_result_block(parent))
                await self._k._append_results(task, blocks)
                await self._k._consume_durable_call(call_id)
        except Exception as exc:  # broad-exception: continuation boundary converts durable-store/binding failures into truthful task outcomes
            await self._k._release_durable_call(call_id)
            await self._k._append_results(
                task,
                [
                    CapabilityResultBlock(
                        call_id=call_id,
                        capability_id=request.capability_id,
                        ok=False,
                        error=f"approval continuation failed: {exc}",
                    )
                ],
            )
        return None

    async def _reconcile_workflow_continuation(
        self,
        record,
        result,
        *,
        workspace_root: str | None = None,
        workspace=None,
    ) -> None:
        """Advance a workflow step when its canonical approval call completes."""
        if self._k._workflow_run_store is None:
            return
        policy_context = record.get("policy_context") or {}
        if not policy_context.get("workflow_run_id"):
            return
        failures: tuple[str, ...] = ()
        result_status = getattr(result, "status", None)
        result_status = getattr(result_status, "value", result_status)
        if result_status is None:
            result_status = "ok" if getattr(result, "ok", False) else "failed"
        if result_status != "ok":
            failures = (getattr(result, "error", None) or "approved call failed",)
        completion = {
            "output": getattr(result, "output", None),
            "failures": failures,
            "workspace_root": workspace_root,
        }
        if workspace is not None:
            completion["workspace"] = workspace
        await self._k._workflow_run_store.complete_call(
            str(record.get("call_id") or ""),
            **completion,
        )

    async def _consume_durable_call(self, call_id: str) -> None:
        if self._k._continuation_store is None:
            return
        try:
            await self._k._continuation_store.mark_consumed_for_call(call_id)
        except Exception as exc:  # broad-exception: continuation boundary converts durable-store/binding failures into truthful task outcomes
            _logger.warning("continuation consume failed for %s: %s", call_id, exc)

    async def _release_durable_call(self, call_id: str) -> None:
        if self._k._continuation_store is None:
            return
        release = getattr(self._k._continuation_store, "release_claim", None)
        if release is None:
            return
        try:
            await release(call_id)
        except Exception as exc:  # broad-exception: continuation boundary converts durable-store/binding failures into truthful task outcomes
            _logger.warning("continuation claim release failed for %s: %s", call_id, exc)

    async def _scrub_input_answer(self, request_id: str, answer_ref: str) -> None:
        """Replace a stored input answer with its non-secret ref.

        Called after a runtime secret has been moved to the SecretManager.
        Keeps the durable row for audit but removes the raw value.
        """
        if self._k._input_request_store is None:
            return
        try:
            await self._k._scrub_input_answer_impl(request_id, answer_ref)
        except Exception as exc:  # broad-exception: continuation boundary converts durable-store/binding failures into truthful task outcomes
            _logger.warning("input answer scrub failed for %s: %s", request_id, exc)

    async def _scrub_input_answer_impl(self, request_id: str, answer_ref: str) -> None:
        store = self._k._input_request_store
        await store.ensure_table()
        await store._db.execute(
            "UPDATE input_requests SET answer = NULL, answer_ref = ? "
            "WHERE id = ? AND answer_ref IS NULL",
            (answer_ref, request_id),
        )

    # ------------------------------------------------------------------
    # Approval/input/park/resume cluster (P1-10 extraction, second slice).
    # ------------------------------------------------------------------
    async def _approval_path(self, task, state, outcome: DispatchResult) -> TaskResult | None:
        await self._k._transition(task, TaskStatus.WAITING_APPROVAL)
        await self._k._emit("ApprovalRequested", {"calls": len(outcome.suspended)}, task)
        woke = await self._k._park_wait(task, state)
        if woke == "cancelled":
            return await self._k._finalize(
                task, state, TaskStatus.CANCELLED, "task cancelled during approval"
            )
        if woke == "slot_released":
            decision = self._k._resume_decision.get(task.id)
            if decision is None:
                decision = await self._durable_approval_decision(task, outcome)
            if decision is not None:
                woke = "resumed"
            else:
                return await self._k._paused_result(
                    task, state, TaskStatus.WAITING_APPROVAL, "awaiting approval decision"
                )
        decision = self._k._resume_decision.get(task.id)
        if decision is None:
            decision = await self._durable_approval_decision(task, outcome)
        decision = decision or "denied"
        try:
            await self._k._transition(task, TaskStatus.RUNNING)
        except Exception:  # broad-exception: continuation boundary converts durable-store/binding failures into truthful task outcomes
            return await self._k._finalize_decision(
                task,
                state,
                TerminationDecision(True, "approval wait could not resume", TaskStatus.BLOCKED),
            )
        if decision in ("denied", "cancelled"):
            denied = [_deny_result(s) for s in outcome.suspended]
            parent_results: list[CapabilityResultBlock] = []
            parent_suspended: list[SuspendedCall] = []
            for suspended_call, result in zip(outcome.suspended, denied):
                await self._k._reconcile_workflow_suspended(suspended_call, result)
                resume_parent = getattr(self, "_resume_workflow_parent", None)
                parent = (
                    await resume_parent(task, suspended_call) if resume_parent is not None else None
                )
                if isinstance(parent, SuspendedCall):
                    parent_suspended.append(parent)
                elif parent is not None:
                    parent_results.append(_to_result_block(parent))
            await self._k._append_results(task, [*denied, *parent_results])
            await self._k._mark_continuations_consumed(outcome.suspended)
            if parent_suspended:
                return await self._k._approval_path(
                    task, state, DispatchResult(suspended=tuple(parent_suspended))
                )
            return None
        if self._k._dispatch_factory is None:
            return None
        shim = self._k._dispatch_factory(task)
        suspended = list(outcome.suspended)
        while suspended:
            requests = [s.request for s in suspended]
            for request in requests:
                object.__setattr__(request, "origin", CapabilityRequestOrigin.TRUSTED_ORCHESTRATION)
            items = await shim.resume_requests(
                task,
                requests,
                **_replay_policy_context(task),
                runtime_remaining_s=_remaining_runtime_seconds(task, state),
                verification_environment=getattr(task, "verification_environment", None),
                _directives_by_call_id={
                    suspended_call.call_id: suspended_call.directives
                    for suspended_call in suspended
                    if suspended_call.directives is not None
                },
            )
            results = []
            raw_results = []
            re_ask: list = []
            for it in [*items.results, *items.suspended]:
                if isinstance(it, SuspendedCall):
                    re_ask.append(it)
                else:
                    raw_results.append(it)
                    results.append(_to_result_block(it))
            by_call_id = {item.call_id: item for item in suspended}
            for item in raw_results:
                matched_suspended = by_call_id.get(getattr(item, "call_id", ""))
                if matched_suspended is not None:
                    reconcile_kwargs = {"workspace_root": shim.scope.workspace.root}
                    if (
                        "workspace"
                        in inspect.signature(self._k._reconcile_workflow_suspended).parameters
                    ):
                        reconcile_kwargs["workspace"] = shim.scope.workspace
                    await self._k._reconcile_workflow_suspended(
                        matched_suspended, item, **reconcile_kwargs
                    )
                    resume_parent = getattr(self, "_resume_workflow_parent", None)
                    parent_result = (
                        await resume_parent(
                            task,
                            matched_suspended,
                            shim=shim,
                        )
                        if resume_parent is not None
                        else None
                    )
                    if isinstance(parent_result, SuspendedCall):
                        re_ask.append(parent_result)
                    elif parent_result is not None:
                        results.append(_to_result_block(parent_result))
            if re_ask:
                await self._k._append_results(task, results)
                return await self._k._approval_path(
                    task, state, DispatchResult(results=(), suspended=tuple(re_ask))
                )
            await self._k._append_results(task, results)
            await self._k._mark_continuations_consumed(suspended)
            return None
        return None

    async def _input_request_path(self, task, state, response, input_calls):
        """Park the task in WAITING_INPUT and return a resumable outcome.

        Persisted: task id, question, choices, expected-input metadata, and the
        continuation identity. When no input store is configured the kernel
        still answers the call truthfully (failed result) instead of parking.
        """
        call = input_calls[0]
        args = dict(call.arguments or {})
        question = str(args.get("question") or "").strip()
        if not question:
            await self._k._append_results(
                task,
                [
                    CapabilityResultBlock(
                        call_id=call.call_id,
                        capability_id="request_input",
                        ok=False,
                        error="request_input requires a non-empty question",
                    )
                ],
                calls=[call],
            )
            return None
        if self._k._input_request_store is None:
            await self._k._append_results(
                task,
                [
                    CapabilityResultBlock(
                        call_id=call.call_id,
                        capability_id="request_input",
                        ok=False,
                        error="operator input is unavailable in this deployment",
                    )
                ],
                calls=[call],
            )
            return None
        # ``record`` makes the question externally answerable.  Arm before
        # that authority commit so an answer cannot be delivered into a gap
        # before the in-memory wait is prepared.
        self._arm_resume_wait(task.id)
        request_id = await self._k._input_request_store.record(
            task_id=task.id,
            session_id=task.session_id,
            question=question,
            choices=tuple((str(c) for c in args.get("choices") or ())),
            context=dict(args.get("context") or {}),
            expected=str(args.get("expected") or "text"),
        )
        extra_calls = [c for c in input_calls if c is not call]
        if extra_calls:
            await self._k._append_results(
                task,
                [
                    CapabilityResultBlock(
                        call_id=c.call_id,
                        capability_id=c.capability_id,
                        ok=False,
                        error="superseded by request_input for this turn",
                    )
                    for c in extra_calls
                ],
                calls=extra_calls,
            )
        await self._k._append_results(
            task,
            [
                CapabilityResultBlock(
                    call_id=call.call_id,
                    capability_id="request_input",
                    ok=True,
                    output=f"waiting for operator input: {request_id}",
                    metadata={"operation": "request_input", "request_id": request_id},
                )
            ],
            calls=[call],
        )
        await self._k._transition(task, TaskStatus.WAITING_INPUT)
        await self._k._emit(
            "InputRequested",
            {
                "request_id": request_id,
                "question": question,
                "choices": [str(c) for c in args.get("choices") or ()],
            },
            task,
        )
        woke = await self._k._park_wait(task, state)
        if woke == "cancelled":
            await self._k._input_request_store.resolve(request_id, "")
            return await self._k._finalize(
                task, state, TaskStatus.CANCELLED, "task cancelled while awaiting input"
            )
        if woke == "slot_released":
            if await self._k._input_request_store.pending_resumable(task.id) is not None:
                return await self._k._consume_pending_input(task, state, request_id, args)
            return await self._k._paused_result(
                task, state, TaskStatus.WAITING_INPUT, f"awaiting operator input: {request_id}"
            )
        return await self._k._consume_pending_input(task, state, request_id, args)

    async def _park_wait(self, task, state) -> str:
        """Park until resume, cancellation, or the slot-release deadline.

        Returns "resumed" | "cancelled" | "slot_released". The deadline is
        the P1-17 worker slot release: a parked task must not pin a worker
        coroutine for the hours an operator may take to answer. Past the
        deadline the caller ends the run with the task in its paused status;
        the durable continuation (open question / pending approval) is what
        wakes the task again.
        """
        ev = self._k._resume.setdefault(task.id, asyncio.Event())
        lock = self._resume_lock(task.id)
        async with lock:
            if state.cancel.is_set():
                self._disarm_resume_wait(task.id)
                return "cancelled"
            # Durable state is authoritative.  The event is only a fast path
            # and may already be set because the decision arrived between
            # arming and this call.
            durable_ready = await self._durable_resume_ready(task)
            if durable_ready or (ev.is_set() and not self._has_durable_source(task)):
                self._disarm_resume_wait(task.id)
                return "resumed"
        resume_task = asyncio.create_task(ev.wait())
        cancel_task = asyncio.create_task(state.cancel.wait())
        wait_s = getattr(self._k, "_parked_slot_wait_s", 300.0)
        timeout_task = asyncio.create_task(asyncio.sleep(wait_s))
        try:
            await asyncio.wait(
                {resume_task, cancel_task, timeout_task}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for pending in (resume_task, cancel_task, timeout_task):
                if not pending.done():
                    pending.cancel()
        async with self._resume_lock(task.id):
            if state.cancel.is_set():
                self._disarm_resume_wait(task.id)
                return "cancelled"
            # Re-check after BOTH wakeup and timeout.  This closes the slot
            # release boundary: an answer/approval committed concurrently with
            # timeout is consumed by this run or handed to a relaunch, never
            # stranded behind a cleared event.
            durable_ready = await self._durable_resume_ready(task)
            if durable_ready or (resume_task.done() and not self._has_durable_source(task)):
                self._disarm_resume_wait(task.id)
                return "resumed"
            self._disarm_resume_wait(task.id)
            release = getattr(self._k, "_release_parked_resources", None)
            if callable(release):
                await release(task)
            return "slot_released"

    def _arm_resume_wait(self, task_id: str) -> None:
        arm = getattr(self._k, "_arm_resume_wait", None)
        if arm is not None:
            arm(task_id)
            return
        # Small test doubles that exercise this mechanism directly predate the
        # kernel helper.  Preserve their behavior without creating a second
        # authority path.
        getattr(self._k, "_resume", {}).setdefault(task_id, asyncio.Event()).clear()

    def _disarm_resume_wait(self, task_id: str) -> None:
        armed = getattr(self._k, "_resume_armed", None)
        if armed is not None:
            armed.discard(task_id)

    def _resume_lock(self, task_id: str):
        locks = getattr(self._k, "_resume_locks", None)
        if not isinstance(locks, ReferenceCountedKeyedLocks):
            locks = ReferenceCountedKeyedLocks()
            setattr(self._k, "_resume_locks", locks)
        return locks.lock(task_id)

    async def _durable_resume_ready(self, task) -> bool:
        input_store = getattr(self._k, "_input_request_store", None)
        if input_store is not None:
            try:
                if await input_store.pending_resumable(task.id) is not None:
                    return True
            except Exception as exc:  # broad-exception: continuation boundary converts durable-store/binding failures into truthful task outcomes
                _logger.warning("input resume readiness lookup failed for %s: %s", task.id, exc)

        continuations = getattr(self._k, "_continuation_store", None)
        if continuations is not None:
            ready = getattr(continuations, "resolved_unconsumed_for_task", None)
            if ready is not None:
                try:
                    if await ready(task.id):
                        return True
                except Exception as exc:  # broad-exception: continuation boundary converts durable-store/binding failures into truthful task outcomes
                    _logger.warning(
                        "approval resume readiness lookup failed for %s: %s", task.id, exc
                    )
        return False

    def _has_durable_source(self, task) -> bool:
        return bool(
            getattr(self._k, "_input_request_store", None)
            or getattr(self._k, "_continuation_store", None)
        )

    async def _durable_approval_decision(self, task, outcome: DispatchResult) -> str | None:
        """Read a resolved decision when the in-memory notification is absent."""
        continuations = getattr(self._k, "_continuation_store", None)
        if continuations is None:
            return None
        ready = getattr(continuations, "resolved_unconsumed_for_task", None)
        if ready is None:
            return None
        try:
            rows = await ready(task.id, records=True)
        except TypeError:
            # Older test doubles expose only the boolean readiness signature;
            # the durable production store supports record retrieval.
            if await ready(task.id):
                return self._k._resume_decision.get(task.id)
            return None
        by_call_id = {item.call_id for item in outcome.suspended}
        for row in rows or ():
            if row.get("call_id") in by_call_id:
                decision = row.get("decision")
                if decision:
                    return str(decision)
        return None

    async def _resume_paused_entry(self, task, state) -> TaskResult | None:
        """Resume-or-re-park entry for a relaunched paused task (P1-17).

        Runs once at loop entry. Handles the durable states a task can be
        relaunched in:

        - ANSWERED_PENDING_RESUME input request → consume the answer (same
          authority path as the live wakeup) and continue into the loop.
        - OPEN input request → re-park (bounded) for the answer.
        - Unresolved approval continuation → re-park; a resolved one is left
          for _resume_durable_continuation, which already owns that path.

        Returns a TaskResult only when the run should end here (cancelled,
        or re-parked past the slot deadline); None continues into the loop.
        """
        input_store = getattr(self._k, "_input_request_store", None)
        if input_store is None:
            return None
        # Arm before checking durable state. The request existed before this
        # process restarted and is already externally answerable.
        self._arm_resume_wait(task.id)
        try:
            answered = await input_store.pending_resumable(task.id)
        except Exception:  # broad-exception: continuation boundary converts durable-store/binding failures into truthful task outcomes
            answered = None
        if answered is not None:
            request_id = str(answered.get("id") or "")
            if request_id:
                return await self._k._consume_pending_input(task, state, request_id)
            return None
        try:
            open_request = await input_store.pending_for_task(task.id)
        except Exception:  # broad-exception: continuation boundary converts durable-store/binding failures into truthful task outcomes
            open_request = None
        if open_request is None:
            return None
        await self._k._transition(task, TaskStatus.WAITING_INPUT)
        await self._k._emit(
            "InputRequested",
            {
                "request_id": open_request.get("id"),
                "question": open_request.get("question"),
                "choices": list(open_request.get("choices") or ()),
                "repark": True,
            },
            task,
        )
        woke = await self._k._park_wait(task, state)
        if woke == "cancelled":
            await self._k._input_request_store.resolve(str(open_request["id"]), "")
            return await self._k._finalize(
                task, state, TaskStatus.CANCELLED, "task cancelled while awaiting input"
            )
        if woke == "slot_released":
            return await self._k._paused_result(
                task,
                state,
                TaskStatus.WAITING_INPUT,
                f"awaiting operator input: {open_request.get('id')}",
            )
        return await self._k._consume_pending_input(task, state, str(open_request["id"]))

    async def _consume_pending_input(
        self, task, state, request_id: str, args: dict | None = None
    ) -> TaskResult | None:
        """Read the durable answer, apply secret-opacity rules, append the
        user turn, and return to the loop (None = continue).

        Shared by the live wakeup path and the relaunched-loop entry, so a
        task relaunched after slot release (or process restart) consumes the
        answer exactly once through the same authority path.
        """
        args = args or {}
        durable = await self._k._input_request_store.pending_resumable(task.id)
        if durable is None:
            # A second runner must not consume or append the same operator
            # answer.  Durable input state, not the in-memory event, decides.
            return None
        answer = str((durable or {}).get("answer") or "")
        answer_ref = (durable or {}).get("answer_ref") or None
        expected = str((durable or {}).get("expected") or "text").lower()
        if expected == "secret" and self._k._secret_manager is not None:
            secret_name = (
                (durable or {}).get("context", {}).get("secret_name")
                or (args.get("context") or {}).get("secret_name")
                or "operator_secret"
            )
            answer_ref = self._k._secret_manager.store_task_secret(
                task.id, name=secret_name, value=answer
            )
            await self._k._scrub_input_answer(request_id, answer_ref)
            answer = f"Credential '{secret_name}' is now available for this task."
        elif task.session_id:
            try:
                await self._k._messages.append_user_turn(
                    task.session_id,
                    Message(
                        id=f"msg_input_{request_id}",
                        role=Role.USER,
                        blocks=(TextBlock(text=answer),),
                        created_at=utcnow(),
                        provenance=Provenance(source_type=SourceType.USER),
                        metadata={
                            "task_id": task.id,
                            "input_request_id": request_id,
                            "canonical_user_turn": False,
                        },
                    ),
                )
            except Exception:  # broad-exception: continuation boundary converts durable-store/binding failures into truthful task outcomes
                _logger.warning("input answer persistence failed for %s", request_id, exc_info=True)
        consumed = await self._k._input_request_store.consume(request_id)
        if consumed is False:
            return None
        try:
            row = await self._k._task_store.get(task.id)
            if row and (row.get("status") or "").upper() == TaskStatus.WAITING_INPUT.value:
                await self._k._transition(task, TaskStatus.RUNNING)
        except Exception:  # broad-exception: continuation boundary converts durable-store/binding failures into truthful task outcomes
            return await self._k._finalize_decision(
                task,
                state,
                TerminationDecision(True, "input wait could not resume", TaskStatus.BLOCKED),
            )
        await self._k._emit("InputReceived", {"request_id": request_id}, task)
        return None

    # ------------------------------------------------------------------ #
    # Finalization
    # ------------------------------------------------------------------ #


def _deny_result_for_request(request: CapabilityRequest) -> CapabilityResultBlock:
    return CapabilityResultBlock(
        call_id=request.call_id,
        capability_id=request.capability_id,
        ok=False,
        error="denied: approval not granted",
    )


def _to_result_block(result) -> CapabilityResultBlock:
    from athena.protocol.capabilities import CapabilityResultStatus

    if isinstance(result, CapabilityResultBlock):
        return result
    return CapabilityResultBlock(
        call_id=getattr(result, "call_id", ""),
        capability_id=getattr(result, "capability_id", ""),
        ok=(getattr(result, "status", None) is CapabilityResultStatus.OK),
        output=getattr(result, "output", "") or "",
        error=getattr(result, "error", None),
        metadata=getattr(result, "metadata", None) or {},
        ref_uri=getattr(result, "ref_uri", None),
    )
