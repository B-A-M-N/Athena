"""Fallback-context mechanics for :mod:`athena.kernel.inference_broker`.

The broker decides whether a provider failure is retryable and owns the
attempt loop.  This module only prepares the next candidate and, when a
smaller context window requires it, recompiles the request for that candidate.
It does not decide whether inference should happen.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from athena.context.compiler import CompiledContext
from athena.protocol.errors import ContextOverflow, ProviderError
from athena.models.router import ModelSelection
from athena.protocol.tasks import TaskSpec

if TYPE_CHECKING:
    from athena.kernel.kernel import AgentKernel


async def prepare_fallback(
    kernel: AgentKernel,
    task: TaskSpec,
    compiled: CompiledContext,
    *,
    attempted: frozenset[str | tuple[str, str]],
    error: ProviderError,
) -> tuple[ModelSelection, CompiledContext]:
    """Select the next model and narrow compiled context when required.

    Retry eligibility, attempt limits, and the decision to call this helper
    remain in ``InferenceBroker``.  The kernel remains the injected router and
    compiler owner; this function merely coordinates those existing ports.
    """
    selection = await kernel._select_model(
        task,
        compiled,
        exclude=attempted,
        relax_context=isinstance(error, ContextOverflow),
    )
    if isinstance(error, ContextOverflow):
        fallback_limit = getattr(selection.info, "context_limit", None)
        current_need = getattr(
            compiled.requirements,
            "minimum_context_window_tokens",
            None,
        )
        if (
            fallback_limit is not None
            and current_need is not None
            and fallback_limit < current_need
        ):
            compiled = await kernel._compile(task, context_window=int(fallback_limit))
    return selection, compiled
