"""Durable one-shot retry of a repaired generated operation.

The mechanism preserves the canonical call identity and uncertain-outcome
boundary around the kernel-provided dispatch callback. It does not repair
source, choose a repair, or finalize the task; those decisions remain with
AgentKernel and the synthesis capability.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from athena.protocol.ids import new_id
from athena.protocol.messages import CapabilityCallBlock, CapabilityResultBlock

__all__ = ["GeneratedRetryMechanism"]

_logger = logging.getLogger("athena.kernel.generated_retry")


class GeneratedRetryMechanism:
    """Execute and durably account the original call after a successful repair."""

    def __init__(
        self,
        *,
        update_metadata: Callable[..., Awaitable[Any]],
        emit: Callable[..., Awaitable[None]],
    ) -> None:
        self._update_metadata = update_metadata
        self._emit = emit

    async def retry(
        self,
        task: Any,
        state: Any,
        results: tuple[CapabilityResultBlock, ...],
        dispatch: Callable[[Any, list[CapabilityCallBlock]], Awaitable[Any]],
    ) -> Any:
        if state.generated_recovery_retried or state.generated_recovery_original is None:
            return None
        repaired_id = next(
            (
                str((result.metadata or {}).get("capability_id"))
                for result in results
                if result.ok
                and result.capability_id == "synthesis"
                and (result.metadata or {}).get("capability_id")
            ),
            None,
        )
        if not repaired_id:
            return None
        original = state.generated_recovery_original
        retry_call = CapabilityCallBlock(
            call_id=new_id("call"),
            capability_id=repaired_id,
            arguments=dict(original.get("arguments") or {}),
        )
        state.generated_recovery_retried = True
        state.generated_recovery_retry_status = "dispatching"
        await self._update_metadata(
            task.id,
            {
                "generated_recovery_retried": True,
                "generated_recovery_original": original,
                "generated_recovery_retry_status": "dispatching",
            },
        )
        await self._emit(
            "GeneratedRepairRetried",
            {
                "original_capability_id": original.get("capability_id"),
                "repaired_capability_id": repaired_id,
                "original_call_id": original.get("call_id"),
            },
            task,
        )
        try:
            outcome = await dispatch(task, [retry_call])
        except BaseException as exc:  # noqa: BLE001 - persist uncertainty before propagating dispatch failure
            state.generated_recovery_retry_status = "uncertain"
            state.generated_recovery_retry_error = str(exc)
            await self._update_metadata(
                task.id,
                {
                    "generated_recovery_retry_status": "uncertain",
                    "generated_recovery_retry_error": str(exc),
                },
            )
            raise
        state.generated_recovery_retry_status = "consumed"
        state.generated_recovery_retry_error = None
        await self._update_metadata(
            task.id,
            {
                "generated_recovery_retry_status": "consumed",
                "generated_recovery_retry_error": None,
            },
        )
        return outcome
