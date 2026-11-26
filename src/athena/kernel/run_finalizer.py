"""Run finalizer — terminal/paused result assembly (P1-10).

Extracted from AgentKernel. Mechanism, not a second authority: reality
compensation, the lifecycle's canonical finalize, usage summaries, and
message persistence all resolve through the bound :class:`AgentKernel`
instance (``self._k``). The kernel alone decides WHEN a run ends; this
module assembles HOW the terminal or paused boundary is written.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from athena.evidence import result_qualifies_as_work_evidence
from athena.kernel.termination import TerminationDecision
from athena.protocol.artifacts import ArtifactRef
from athena.protocol.messages import ArtifactRefBlock, CapabilityResultBlock
from athena.protocol.models import ModelResponse
from athena.protocol.tasks import TaskResult, TaskSpec, TaskStatus, UsageSummary


from athena.kernel.messages import assistant_message, results_message as _base_results_message


def _assistant_message(task, response):
    return assistant_message(task, response)


def _results_message(task, blocks):
    # A visual capability result is durable as a normal result plus a typed
    # artifact reference.  Keeping the image outside the result text avoids
    # putting pixels in the transcript while allowing provider adapters to
    # hydrate a model-visible image input on the next turn.
    expanded = []
    for block in blocks:
        expanded.append(block)
        if not isinstance(block, CapabilityResultBlock):
            continue
        metadata = dict(block.metadata or {})
        raw_ref = metadata.get("artifact_ref")
        if not isinstance(raw_ref, dict) or not str(metadata.get("mime_type", "")).startswith(
            "image/"
        ):
            continue
        uri = str(block.ref_uri or raw_ref.get("uri") or "")
        if not uri:
            continue
        expanded.append(
            ArtifactRefBlock(
                uri=uri,
                ref=ArtifactRef(
                    id=str(raw_ref.get("id") or uri),
                    uri=uri,
                    hash=raw_ref.get("hash"),
                    mime_type=raw_ref.get("mime_type") or metadata.get("mime_type"),
                    size=raw_ref.get("size"),
                    storage_path=raw_ref.get("storage_path"),
                    producer=raw_ref.get("producer"),
                    task_id=raw_ref.get("task_id") or task.id,
                    metadata=raw_ref.get("metadata") or {},
                ),
            )
        )
    return _base_results_message(task, expanded)


if TYPE_CHECKING:
    from athena.kernel.kernel import AgentKernel

__all__ = ["RunFinalizer"]

_logger = logging.getLogger("athena.kernel")


class RunFinalizer:
    """Terminal/paused boundary mechanism owned by AgentKernel."""

    def __init__(self, kernel: AgentKernel) -> None:
        self._k = kernel

    async def _finalize(self, task, state, status: TaskStatus, reason: str) -> TaskResult:
        if self._k._reality_coordinator is not None:
            cleanup_status = await self._k._reality_coordinator.discard_incomplete(task.id, status)
            if cleanup_status is TaskStatus.RECOVERY_REQUIRED:
                status = TaskStatus.RECOVERY_REQUIRED
                reason = f"{reason}; reality compensation requires recovery"
        return await self._k._lifecycle.finalize(
            task,
            status=status,
            reason=reason,
            usage=UsageSummary(
                input_tokens=state.input_tokens,
                output_tokens=state.output_tokens,
                model_calls=state.model_calls,
                cost_usd=state.cost,
                cost_known=state.cost_known,
                duration_ms=state.elapsed_ms,
            ),
        )

    async def _finalize_decision(self, task, state, decision: TerminationDecision) -> TaskResult:
        completion = None
        if self._k._reality_coordinator is not None:
            completion = await self._k._reality_coordinator.prepare_completion(task, decision)
            decision = completion.decision
        result = await self._k._lifecycle.finalize(
            task,
            decision=decision,
            usage=UsageSummary(
                input_tokens=state.input_tokens,
                output_tokens=state.output_tokens,
                model_calls=state.model_calls,
                cost_usd=state.cost,
                cost_known=state.cost_known,
                duration_ms=state.elapsed_ms,
            ),
        )
        if completion is not None and completion.committed:
            try:
                await self._k._reality_coordinator.mark_finalized(task.id)
            except Exception as exc:  # noqa: BLE001 - task result is already durable
                # The completion journal is deliberately replayable.  Do not
                # turn an already persisted COMPLETE task into an exception
                # merely because the final journal tombstone was interrupted;
                # startup reconciliation will close it on the next run.
                _logger.warning(
                    "could not finalize reality completion journal for %s: %s",
                    task.id,
                    exc,
                )
        return result

    # ------------------------------------------------------------------ #
    # Persistence / event / misc helpers
    # ------------------------------------------------------------------ #

    async def _paused_result(self, task, state, status: TaskStatus, reason: str) -> TaskResult:
        """End the run leaving the task in a paused (non-terminal) status.

        The task keeps its WAITING_* status — NOT terminal — so `wait_for`
        keeps polling and the durable continuation can relaunch it. Usage
        consumed so far is checkpointed without writing a terminal result.
        """
        try:
            if self._k._budgets is not None:
                persist_budget = getattr(self._k._budgets, "_persist_usage", None)
                if persist_budget is not None:
                    await persist_budget(task.id)
        except Exception:  # noqa: BLE001 — paused return must not fail the run
            _logger.warning("budget checkpoint on pause failed for %s", task.id)
        await self._k._emit(
            "TaskSlotReleased",
            {"status": status.value, "reason": reason},
            task,
        )
        return TaskResult(
            task_id=task.id,
            status=status,
            summary=reason,
            usage=UsageSummary(
                input_tokens=state.input_tokens,
                output_tokens=state.output_tokens,
                model_calls=state.model_calls,
                cost_usd=state.cost,
                cost_known=state.cost_known,
                duration_ms=state.elapsed_ms,
            ),
        )

    async def _append_final_response(self, task: TaskSpec, response: ModelResponse) -> None:
        """Persist a terminal text-only assistant answer to the session store.

        The non-terminal path appends assistant responses so resumed sessions
        see the animated transcript. A final answer (no capability calls) was
        previously never stored, so a resumed session missed it. Persist it here,
        guarding only the current-process duplicate-append fast path. Durable
        message receipts remain the correctness boundary across restarts.
        """
        if response.request_id in self._k._response_append_cache:
            return
        message = _assistant_message(task, response)
        if not any((getattr(b, "text", "") or "") for b in message.blocks):
            return
        appended = await self._k._append_assistant_message(message)
        if not appended:
            if response.request_id:
                self._k._response_append_cache.add(response.request_id)
            return
        await self._k._emit(
            "TaskMessage",
            {
                "message_id": message.id,
                "role": message.role.value,
                "text": message.conversation_text(),
            },
            task,
        )
        self._k._response_append_cache.add(response.request_id)

    async def _append_results(self, task: TaskSpec, blocks, *, calls=()) -> None:
        if not blocks:
            return
        state = self._k._runs.get(task.id)
        if state is not None:
            calls_by_id = {getattr(call, "call_id", ""): call for call in calls}
            for block in blocks:
                if not isinstance(block, CapabilityResultBlock):
                    continue
                evidence = result_qualifies_as_work_evidence(
                    block,
                    call=calls_by_id.get(block.call_id),
                )
                if evidence is not None and evidence.call_id not in {
                    item.call_id for item in state.work_evidence
                }:
                    state.work_evidence.append(evidence)
        message = _results_message(task, blocks)
        await self._k._messages.append(message)
        await self._k._emit(
            "TaskMessage",
            {
                "message_id": message.id,
                "role": message.role.value,
                "text": message.conversation_text(),
            },
            task,
        )
