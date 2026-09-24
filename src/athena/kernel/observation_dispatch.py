"""Kernel-side interpreter observation admission mechanics.

The kernel owns whether the primary loop spends one bounded subturn. This
helper prepares candidate observations, applies the existing triggering policy,
tracks repeated failures, and invokes the kernel-provided offer callback at most
once per dispatch. It never dispatches capabilities or finalizes tasks.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from athena.interpreter.triggering import observation_warrants_subturn
from athena.kernel.observation_support import (
    budget_exhausted,
    observation_from_result,
    repeated_failure_observation,
    runtime_completed_observation,
)
from athena.protocol.messages import CapabilityResultBlock

_logger = logging.getLogger("athena.kernel.observation_dispatch")

__all__ = ["ObservationDispatchMechanism"]


class ObservationDispatchMechanism:
    """Offer at most one typed interpreter observation per dispatch."""

    def __init__(self, offer: Callable[[Any, Any, Any], Awaitable[None]]) -> None:
        self._offer = offer

    async def offer_one(
        self,
        *,
        task: Any,
        state: Any,
        results: Sequence[CapabilityResultBlock],
        offer_enabled: bool,
    ) -> None:
        if not offer_enabled:
            return
        budget = getattr(task, "resource_budget", None)
        for result in results:
            if not isinstance(result, CapabilityResultBlock):
                continue
            if result.ok:
                candidates = [runtime_completed_observation(task, result)]
            else:
                failures = state.interpreter_failure_counts
                failures[result.capability_id] = failures.get(result.capability_id, 0) + 1
                candidates = [
                    observation_from_result(task, result),
                    repeated_failure_observation(task, result, failures[result.capability_id]),
                ]
            offered = False
            for observation in candidates:
                if observation is None or not observation_warrants_subturn(observation):
                    continue
                if budget is not None and budget_exhausted(state, budget):
                    break
                try:
                    await self._offer(task, state, observation)
                    offered = True
                except Exception:  # noqa: BLE001 — interpreter fusion cannot kill primary loop
                    _logger.warning(
                        "interpreter fusion failed for %s observation",
                        observation.kind,
                        exc_info=True,
                    )
                break
            if offered:
                return
