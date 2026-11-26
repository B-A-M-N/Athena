"""Composition helpers for model-backed reasoning support.

The service remains the application composition root, but this neutral
mechanism owns repeated router/policy normalization and late-bound auxiliary
callbacks. It does not select the next task action, construct a kernel, or
authorize a capability call.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from athena.protocol.tasks import ModelPolicy

__all__ = ["ReasoningSupport"]

_logger = logging.getLogger("athena.service.reasoning_support")


class ReasoningSupport:
    """Build model router and auxiliary support callbacks from explicit state."""

    def __init__(self, *, usage_store: Any = None) -> None:
        self._usage_store = usage_store

    @staticmethod
    def role_policies(raw: Any) -> dict[str, ModelPolicy]:
        """Normalize configured role mappings into typed model policies."""
        out: dict[str, ModelPolicy] = {}
        for role, spec in dict(raw or {}).items():
            if not isinstance(spec, Mapping):
                _logger.warning("model_roles[%r] ignored: not a table", role)
                continue
            max_cost = None
            raw_cost = spec.get("max_cost_usd")
            if raw_cost is not None:
                try:
                    max_cost = Decimal(str(raw_cost))
                except (InvalidOperation, ValueError):
                    _logger.warning("model_roles[%r].max_cost_usd invalid: %r", role, raw_cost)
            out[str(role)] = ModelPolicy(
                role=str(role),
                allowed=tuple(str(a) for a in (spec.get("allowed") or ()) if a),
                privacy=str(spec.get("privacy") or "local-preferred"),
                require_tools=bool(spec.get("require_tools", False)),
                max_cost_usd=max_cost,
                routing_preference=str(spec.get("routing_preference") or "balanced"),
                min_quality_tier=(
                    str(spec["min_quality_tier"]).strip()
                    if spec.get("min_quality_tier") is not None
                    else None
                ),
                require_declared_quality=bool(spec.get("require_declared_quality", False)),
                max_model_attempts=int(spec.get("max_model_attempts", 2)),
            )
        return out

    @staticmethod
    def make_summarizer(kernel_resolver: Any) -> Any:
        """Build the late-bound auxiliary summarizer callback."""

        async def summarize(text: str, *, task: Any = None, max_tokens: int | None = None):
            kernel = kernel_resolver()
            if kernel is None:
                return None
            prompt = (
                "Summarize the following complete agent-work transcript excerpt "
                "into at most 6 sentences, preserving decisions, file changes, "
                "and unresolved issues. Output ONLY the summary.\n\n" + text
            )
            if max_tokens is not None:
                prompt += f"\n\nKeep the response within {max_tokens} estimated tokens."
            if task is not None:
                return await kernel.task_utility_inference(
                    task=task, system_prompt="", user_prompt=prompt, role="summarizer"
                )
            return await kernel.utility_inference(
                system_prompt="", user_prompt=prompt, role="summarizer"
            )

        return summarize

    @staticmethod
    def make_interpreter(kernel_resolver: Any) -> Any:
        """Build the late-bound interpreter extension callback."""
        from athena.interpreter import InterpreterExtension

        async def broker(*, context, system_prompt, user_prompt):
            kernel = kernel_resolver()
            if kernel is None:
                raise RuntimeError("interpreter broker: kernel not constructed")
            return await kernel.interpreter_subturn(
                context=context,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )

        return InterpreterExtension(inference_broker=broker)
