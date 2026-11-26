"""Auxiliary utility inference mechanics subordinate to the inference broker.

The kernel remains the only decision authority for when model work happens.
This module owns one narrow mechanism: a best-effort auxiliary request that
reuses the kernel's router, provider registry, usage store, and budget ledger.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Mapping

from athena.kernel.tokens import (
    actual_model_cost as _actual_model_cost,
    bookkeeping_failure as _bookkeeping_failure,
    input_tokens_of as _input_tokens_of,
    output_tokens_of as _output_tokens_of,
    worst_case_cost as _worst_case_cost,
)
from athena.models.tokens import ModelTokenEstimator
from athena.protocol.ids import new_id
from athena.protocol.messages import (
    Message,
    Provenance,
    SourceType,
    TrustClass,
    utcnow,
)
from athena.protocol.models import ModelRequest
from athena.protocol.errors import TaskBudgetExceeded

if TYPE_CHECKING:
    from athena.kernel.inference_broker import InferenceBroker

__all__ = ["UtilityInferenceRunner"]

_logger = logging.getLogger("athena.kernel")


class UtilityInferenceRunner:
    """Execute best-effort auxiliary inference through kernel-owned resources."""

    def __init__(self, broker: InferenceBroker) -> None:
        self._broker = broker

    async def run(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        role: str = "summarizer",
        task_id: str | None = None,
        session_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        budget_task_id: str | None = None,
    ) -> str | None:
        """Route one auxiliary request; return text or ``None`` on failure."""
        kernel = self._broker._k
        if kernel._provider_usage_store is None or kernel._registry is None:
            return None

        usage_id: str | None = None
        budget_id = budget_task_id or task_id
        reservation_amount: Decimal | None = None
        model_lease = None
        try:
            from athena.protocol.messages import TextBlock
            from athena.protocol.tasks import ModelPolicy

            selection = await kernel._router.select(
                policy=ModelPolicy(role=role, require_tools=False)
            )
            provider = kernel._registry.provider_for(selection.provider)
            attempt_metadata = self._attempt_metadata(
                selection,
                system_prompt=system_prompt,
                role=role,
                metadata=metadata,
            )
            usage_id = await kernel._provider_usage_store.record_attempt(
                provider=selection.provider,
                model=selection.model,
                task_id=task_id,
                session_id=session_id,
                metadata=attempt_metadata,
            )
            request = self._request(
                selection,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                metadata=attempt_metadata,
            )
            token_estimator = ModelTokenEstimator.from_profile(
                kernel._registry.model_profile_for(selection.provider, selection.model)
            )
            reservation_amount, model_lease = await self._reserve(
                budget_id,
                request=request,
                selection=selection,
                estimator=token_estimator,
            )
            parts: list[str] = []
            async with kernel._utility_model_semaphore:
                async for event in provider.complete(request):
                    if getattr(event, "type", None) is None or event.type.value != "done":
                        continue
                    response = event.response
                    if response is None:
                        continue
                    for block in response.blocks:
                        if isinstance(block, TextBlock) and block.text:
                            parts.append(block.text)
                    actual_cost = await self._account_success(
                        budget_id,
                        request=request,
                        response=response,
                        selection=selection,
                        estimator=token_estimator,
                        reservation_amount=reservation_amount,
                    )
                    if reservation_amount is not None:
                        reservation_amount = None
                    usage_id = await self._record_success(
                        usage_id,
                        response=response,
                        actual_cost=actual_cost,
                        role=role,
                        task_id=task_id,
                        metadata=metadata,
                    )
            if model_lease is not None:
                await model_lease.__aexit__(None, None, None)
                model_lease = None
            if reservation_amount is not None and budget_id and kernel._budgets is not None:
                await kernel._budgets.release_model_cost(budget_id, reservation_amount)
                reservation_amount = None
            if usage_id is not None:
                await self._record_no_done(
                    usage_id,
                    role=role,
                    task_id=task_id,
                    metadata=metadata,
                )
            return " ".join(parts).strip() or None
        except Exception:
            await self._cleanup_failure(
                usage_id,
                budget_id=budget_id,
                reservation_amount=reservation_amount,
                model_lease=model_lease,
                role=role,
                task_id=task_id,
                metadata=metadata,
            )
            return None

    def _attempt_metadata(
        self,
        selection,
        *,
        system_prompt: str,
        role: str,
        metadata: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        from athena.models.compat.caching import build_cache_key, cache_fingerprint

        kernel = self._broker._k
        attempt_metadata = dict(metadata or {})
        attempt_metadata.update(
            {
                "role": role,
                "purpose": attempt_metadata.get("purpose", "utility_inference"),
                "state": "started",
            }
        )
        inference_metadata = kernel._inference_metadata(selection)
        namespace = kernel._trusted_cache_namespace()
        stable_payload = (
            [{"role": "system", "block_types": ["text"], "content": system_prompt}]
            if system_prompt
            else []
        )
        attempt_metadata.update(
            {
                **inference_metadata,
                "cache_namespace": namespace,
                "cache_session_key": build_cache_key(
                    namespace=namespace,
                    provider=selection.provider,
                    model=selection.model,
                    profile_fingerprint=str(
                        inference_metadata.get(
                            "provider_profile_fingerprint",
                            inference_metadata.get("provider_profile_id", selection.provider),
                        )
                    ),
                    prefix_fingerprint=cache_fingerprint(stable_payload),
                ),
                "cache_prefix_message_count": len(stable_payload),
            }
        )
        return attempt_metadata

    def _request(
        self,
        selection,
        *,
        system_prompt: str,
        user_prompt: str,
        metadata: Mapping[str, Any],
    ) -> ModelRequest:
        from athena.protocol.messages import Role, TextBlock

        messages: list[Message] = []
        if system_prompt:
            messages.append(
                Message(
                    id=new_id("msg"),
                    role=Role.SYSTEM,
                    blocks=(TextBlock(text=system_prompt),),
                    created_at=utcnow(),
                    provenance=Provenance(
                        source_type=SourceType.SYSTEM,
                        trust=TrustClass.CONFIGURED_INSTRUCTION,
                    ),
                )
            )
        messages.append(
            Message(
                id=new_id("msg"),
                role=Role.USER,
                blocks=(TextBlock(text=user_prompt),),
                created_at=utcnow(),
                provenance=Provenance(
                    source_type=SourceType.SYSTEM,
                    trust=TrustClass.CONFIGURED_INSTRUCTION,
                ),
            )
        )
        return ModelRequest(
            messages=tuple(messages),
            model=selection.model,
            provider=selection.provider,
            request_id=new_id("sum"),
            metadata=dict(metadata),
        )

    async def _reserve(
        self,
        budget_id: str | None,
        *,
        request: ModelRequest,
        selection,
        estimator: ModelTokenEstimator,
    ) -> tuple[Decimal | None, Any | None]:
        """Reserve auxiliary cost and enter the model-call lease."""
        kernel = self._broker._k
        if kernel._budgets is None or not budget_id:
            return None, None
        remaining = await kernel._budgets.remaining(budget_id)
        worst_cost = _worst_case_cost(selection.info, request, estimator=estimator)
        if worst_cost is None and remaining.get("cost_usd") is not None:
            raise TaskBudgetExceeded("utility model pricing unknown under hard monetary budget")
        if (
            worst_cost is not None
            and remaining.get("cost_usd") is not None
            and worst_cost > remaining["cost_usd"]
        ):
            raise TaskBudgetExceeded(
                f"utility model call cost {worst_cost} exceeds remaining budget"
            )
        if worst_cost is not None:
            await kernel._budgets.reserve_model_cost(budget_id, worst_cost)
        model_lease = kernel._budgets.model_call_lease(budget_id)
        await model_lease.__aenter__()
        return worst_cost, model_lease

    async def _account_success(
        self,
        budget_id: str | None,
        *,
        request: ModelRequest,
        response,
        selection,
        estimator: ModelTokenEstimator,
        reservation_amount: Decimal | None,
    ):
        """Consume token accounting and reconcile the auxiliary reservation."""
        kernel = self._broker._k
        if kernel._budgets is None or not budget_id:
            return None
        actual_cost = _actual_model_cost(
            selection.info,
            response,
            request,
            estimator=estimator,
        )
        kernel._budgets.consume(
            budget_id,
            input_tokens=_input_tokens_of(response, request, estimator=estimator),
            output_tokens=_output_tokens_of(response),
            model_calls=1,
        )
        if reservation_amount is not None:
            if actual_cost is None:
                await kernel._budgets.release_model_cost(budget_id, reservation_amount)
            else:
                await kernel._budgets.reconcile_model_cost(
                    budget_id,
                    reserved=reservation_amount,
                    actual=actual_cost,
                )
        persist_budget = getattr(kernel._budgets, "_persist_usage", None)
        if persist_budget is not None:
            await persist_budget(budget_id)
        return actual_cost

    async def _record_success(
        self,
        usage_id: str | None,
        *,
        response,
        actual_cost,
        role: str,
        task_id: str | None,
        metadata: Mapping[str, Any] | None,
    ) -> str | None:
        kernel = self._broker._k
        usage = getattr(response, "usage", None)
        completion_metadata = dict(metadata or {})
        completion_metadata.update(
            {
                "role": role,
                "purpose": completion_metadata.get("purpose", "utility_inference"),
                "state": "success",
            }
        )
        try:
            await kernel._provider_usage_store.record_completion(
                usage_id,
                input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
                output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
                cost_usd=(
                    str(getattr(usage, "cost_usd"))
                    if getattr(usage, "cost_usd", None) is not None
                    else None
                ),
                metadata=completion_metadata,
            )
            return None
        except Exception as exc:
            # P1-11: usage evidence must not vanish silently.
            _bookkeeping_failure("utility usage completion record", task_id, exc)
            return usage_id

    async def _record_no_done(
        self,
        usage_id: str,
        *,
        role: str,
        task_id: str | None,
        metadata: Mapping[str, Any] | None,
    ) -> None:
        kernel = self._broker._k
        try:
            failure_metadata = dict(metadata or {})
            failure_metadata.update(
                {"role": role, "purpose": "utility_inference", "state": "no_done_event"}
            )
            await kernel._provider_usage_store.record_completion(
                usage_id,
                input_tokens=0,
                output_tokens=0,
                metadata=failure_metadata,
            )
        except Exception as exc:
            # P1-11: this row is the only evidence the call happened.
            _bookkeeping_failure("utility usage no-done closure", task_id, exc)

    async def _cleanup_failure(
        self,
        usage_id: str | None,
        *,
        budget_id: str | None,
        reservation_amount: Decimal | None,
        model_lease,
        role: str,
        task_id: str | None,
        metadata: Mapping[str, Any] | None,
    ) -> None:
        kernel = self._broker._k
        if model_lease is not None:
            try:
                await model_lease.__aexit__(None, None, None)
            except Exception as exc:
                _logger.debug(
                    "utility inference lease release failed (task=%s): %s",
                    task_id or "?",
                    exc,
                )
        if reservation_amount is not None and budget_id and kernel._budgets is not None:
            try:
                await kernel._budgets.release_model_cost(budget_id, reservation_amount)
            except Exception as exc:
                _bookkeeping_failure("utility budget reservation release", task_id, exc)
        if usage_id is not None:
            try:
                failure_metadata = dict(metadata or {})
                failure_metadata.update(
                    {"role": role, "purpose": "utility_inference", "state": "error"}
                )
                await kernel._provider_usage_store.record_completion(
                    usage_id,
                    input_tokens=0,
                    output_tokens=0,
                    metadata=failure_metadata,
                )
            except Exception as exc:
                # P1-11: this row is the only evidence the call happened.
                _bookkeeping_failure("utility usage error closure", task_id, exc)
        _logger.debug("utility_inference failed; deterministic fallback", exc_info=True)
        return None
