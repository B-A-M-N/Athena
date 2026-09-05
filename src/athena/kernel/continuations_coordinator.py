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

import logging
from dataclasses import replace
from typing import TYPE_CHECKING

from athena.kernel.dispatch import SuspendedCall
from athena.protocol.capabilities import (
    CapabilityRequest,
    CapabilityRequestOrigin,
    DispatchDirectives,
)
from athena.protocol.ids import new_id
from athena.protocol.messages import CapabilityResultBlock

if TYPE_CHECKING:
    from athena.kernel.kernel import AgentKernel
    from athena.protocol.tasks import TaskSpec


def _replay_policy_context(task) -> dict:
    # Pure static on AgentKernel; imported lazily to avoid the import cycle
    # while keeping one definition.
    from athena.kernel.kernel import AgentKernel

    return AgentKernel._replay_policy_context(task)

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
        dispatcher=None,
        workspace=None,
        profile=None,
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
        if dispatcher is None:
            dispatcher = (
                getattr(self._k._dispatch_factory(task), "_dispatcher", None)
                if self._k._dispatch_factory is not None
                else None
            )
        if workspace is None:
            shim = (
                self._k._dispatch_factory(task) if self._k._dispatch_factory is not None else None
            )
            workspace = getattr(shim, "_workspace", None)
            profile = getattr(shim, "_profile", profile)
        if dispatcher is None or workspace is None:
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
            return await dispatcher.dispatch(
                parent_request,
                workspace=workspace,
                profile=profile,
                **_replay_policy_context(task),
            )
        except Exception as exc:  # the outer result must remain truthful
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
            except Exception as exc:
                _logger.warning("continuation consume failed for %s: %s", call_id, exc)

    async def _resume_durable_continuation(self, task: TaskSpec) -> SuspendedCall | None:
        if self._k._continuation_store is None or self._k._dispatch_factory is None:
            return None
        try:
            record = await self._k._continuation_store.claim_resolved(task.id)
        except Exception as exc:
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
                dispatcher=shim._dispatcher,
                workspace=shim._workspace,
                profile=shim._profile,
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
            replay_context = _replay_policy_context(task)
            result = await shim._dispatcher.dispatch(
                request,
                workspace=shim._workspace,
                profile=shim._profile,
                task_policy=replay_context["task_policy"],
                model_policy=replay_context["model_policy"],
                _directives=directives,
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
                    workspace_root=shim._workspace.root,
                    workspace=shim._workspace,
                )
                parent = await self._k._resume_workflow_parent(
                    task,
                    record=record,
                    dispatcher=shim._dispatcher,
                    workspace=shim._workspace,
                    profile=shim._profile,
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
        except Exception as exc:
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
        except Exception as exc:
            _logger.warning("continuation consume failed for %s: %s", call_id, exc)

    async def _release_durable_call(self, call_id: str) -> None:
        if self._k._continuation_store is None:
            return
        release = getattr(self._k._continuation_store, "release_claim", None)
        if release is None:
            return
        try:
            await release(call_id)
        except Exception as exc:
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
        except Exception as exc:
            _logger.warning("input answer scrub failed for %s: %s", request_id, exc)

    async def _scrub_input_answer_impl(self, request_id: str, answer_ref: str) -> None:
        store = self._k._input_request_store
        await store.ensure_table()
        await store._db.execute(
            "UPDATE input_requests SET answer = NULL, answer_ref = ? "
            "WHERE id = ? AND answer_ref IS NULL",
            (answer_ref, request_id),
        )

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
