"""Deterministic scripted fake model provider (tests / offline default).

This is the canonical implementation. ``athena.models.fake`` re-exports it for
backward compatibility so both import paths reference the SAME class.
"""

from __future__ import annotations

from typing import AsyncIterator, Mapping, TypedDict

from athena.protocol.ids import new_id
from athena.protocol.messages import (
    CapabilityCallBlock,
    CapabilityResultBlock,
    ContentBlock,
    TextBlock,
)
from athena.protocol.models import (
    ModelDelta,
    ModelEvent,
    ModelEventType,
    ModelInfo,
    ModelRequest,
    ModelResponse,
    CostInfo,
    PrivacyClass,
    UsageInfo,
)


class _InfoKwargs(TypedDict, total=False):
    tool_calling: bool
    vision: bool
    context_limit: int | None
    max_output_tokens: int | None
    reasoning: bool
    structured_output: bool
    privacy_class: PrivacyClass
    streaming: bool
    cost: CostInfo | None
    latency_class: str | None


class FakeModelProvider:
    """Deterministic fake model provider for tests and offline reasoning."""

    def __init__(
        self,
        scripts: list[dict] | None = None,
        model: str = "fake-1",
        provider: str = "fake",
        *,
        tool_calling: bool = False,
        vision: bool = False,
        context_limit: int | None = None,
        max_output_tokens: int | None = None,
        reasoning: bool = False,
        structured_output: bool = False,
        privacy_class: PrivacyClass = PrivacyClass.UNKNOWN,
        streaming: bool = True,
        cost: CostInfo | Mapping[str, object] | None = None,
        latency_class: str | None = None,
        response_cost_usd: float | None = None,
    ) -> None:
        self._scripts = list(scripts) if scripts else []
        self._model = model
        self._provider = provider
        if isinstance(cost, Mapping):
            cost = CostInfo(
                per_1m_input=_optional_float(cost.get("per_1m_input")),
                per_1m_output=_optional_float(cost.get("per_1m_output")),
                currency=str(cost.get("currency", "USD")),
            )
        self._response_cost_usd = response_cost_usd
        self._info_kwargs: _InfoKwargs = {
            "tool_calling": tool_calling,
            "vision": vision,
            "context_limit": context_limit,
            "max_output_tokens": max_output_tokens,
            "reasoning": reasoning,
            "structured_output": structured_output,
            "privacy_class": privacy_class,
            "streaming": streaming,
            "cost": cost,
            "latency_class": latency_class,
        }

    async def list_models(self) -> list[ModelInfo]:
        return [
            ModelInfo(
                id=self._model,
                provider=self._provider,
                **self._info_kwargs,
            )
        ]

    async def complete(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        script = self._select_script(request)
        respond = script.get("respond", {}) if isinstance(script, dict) else {}
        text = respond.get("text")
        capability_call = respond.get("capability_call")
        done = bool(respond.get("done", False))

        blocks: list[ContentBlock] = []
        if text is not None:
            blocks.append(TextBlock(type="text", text=text))
        if capability_call is not None and not done:
            cid = (
                capability_call.get("capability_id", "")
                if isinstance(capability_call, dict)
                else ""
            )
            args = capability_call.get("arguments", {}) if isinstance(capability_call, dict) else {}
            blocks.append(
                CapabilityCallBlock(
                    type="capability_call",
                    call_id=new_id("call"),
                    capability_id=cid,
                    arguments=args,
                )
            )

        if text is not None and text != "":
            yield ModelEvent(
                type=ModelEventType.DELTA,
                request_id=request.request_id,
                delta=ModelDelta(request_id=request.request_id, text=text),
            )
        elif capability_call is not None and not done:
            for b in blocks:
                if isinstance(b, CapabilityCallBlock):
                    yield ModelEvent(
                        type=ModelEventType.DELTA,
                        request_id=request.request_id,
                        delta=ModelDelta(request_id=request.request_id, text="", block=b),
                    )

        output_tokens = len(text) if text else 0
        input_tokens = sum(len(msg.text() or "") for msg in request.messages)
        response_cost = respond.get("cost_usd", self._response_cost_usd)
        usage = UsageInfo(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=float(response_cost) if response_cost is not None else None,
        )

        response = ModelResponse(
            request_id=request.request_id,
            model=request.model,
            provider=request.provider,
            blocks=tuple(blocks),
            finish_reason="stop",
            usage=usage,
        )

        yield ModelEvent(
            type=ModelEventType.DONE,
            request_id=request.request_id,
            response=response,
        )

    async def cancel(self, request_id: str) -> None:
        return None

    def _select_script(self, request: ModelRequest) -> dict:
        user_text = ""
        capability_result_ok: bool | None = None
        for msg in request.messages:
            t = msg.text() or ""
            if t:
                user_text = user_text + "\n" + t
            for block in msg.blocks:
                if isinstance(block, CapabilityResultBlock):
                    capability_result_ok = block.ok

        for script in self._scripts:
            if not isinstance(script, dict):
                continue
            match = script.get("match", {}) or {}
            user_contains = match.get("user_contains")
            cap_ok = match.get("capability_result_ok")

            if user_contains is not None and user_contains not in user_text:
                continue
            if cap_ok is not None and capability_result_ok != cap_ok:
                continue
            return script

        return {"respond": {"text": "", "done": True}}


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


__all__ = ["FakeModelProvider"]
