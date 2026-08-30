"""Anthropic Messages provider adapter.

Optionally uses the ``anthropic`` SDK (extra ``athena[anthropic]``); when that
SDK is absent the adapter falls back to the Anthropic REST ``/v1/messages``
endpoint over httpx, which is always available. The class imports with no
network and no SDK present. All Anthropic-specific shape (block types,
``tool_use``, SSE ``content_block_delta``) is confined to this file (INV-006);
in/out remain canonical provider-neutral ModelRequest/ModelEvent types.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
from typing import Any

import httpx

from athena.models.compat.candidates import ToolCallCandidate, record_raw_candidate
from athena.protocol.errors import (
    ContextOverflow,
    ModelUnavailable,
    ProviderAuthenticationError,
    ProviderMalformedResponse,
    ProviderProtocolError,
    ProviderRateLimitError,
    ProviderTimeout,
    ProviderUnavailable,
)
from athena.protocol.ids import new_id
from athena.protocol.messages import (
    ArtifactRefBlock,
    AudioBlock,
    CapabilityCallBlock,
    CapabilityResultBlock,
    ContentBlock,
    FileRefBlock,
    ImageBlock,
    ReasoningBlock,
    Role,
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

_PATH = "/v1/messages"


def _reported_cost_usd(raw: Any) -> float | None:
    """Normalize an optional provider-reported USD cost without inventing 0."""
    if isinstance(raw, Mapping):
        values = raw
    else:
        values = {key: getattr(raw, key, None) for key in ("cost_usd", "cost")}
    for key in ("cost_usd", "cost"):
        value = values.get(key)
        if isinstance(value, Mapping):
            value = value.get("usd", value.get("amount"))
        if value is None:
            continue
        try:
            cost = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(cost) and cost >= 0:
            return cost
    return None


_logger = logging.getLogger("athena.provider.anthropic")


def _load_anthropic():
    try:
        import anthropic

        return anthropic
    except ImportError:
        return None


def _tool_schema(desc) -> dict[str, Any]:
    name = getattr(desc, "id", None)
    if name is None:
        name = str(desc)
    descriptor = getattr(desc, "input_schema", None)
    return {
        "name": name,
        "description": getattr(desc, "description", None) or f"Athena capability {name}",
        "input_schema": descriptor or {"type": "object", "properties": {}},
    }


def _done_event(
    request: ModelRequest,
    *,
    blocks: list[ContentBlock] | tuple[ContentBlock, ...] = (),
    finish: str = "end_turn",
    usage: UsageInfo | None = None,
    metadata: dict[str, Any] | None = None,
) -> ModelEvent:
    request_keys = (
        "task_id",
        "session_id",
        "provider_profile_id",
        "provider_profile_fingerprint",
        "profile_id",
        "model_id",
        "compatibility_profile",
        "model_profile",
        "protocol",
        "tool_repair_mode",
        "max_tool_correction_cycles",
        "cache_mode",
        "cache_session_key",
        "prefix_fingerprint",
        "full_fingerprint",
        "components_fp",
    )
    response_metadata = {
        key: request.metadata[key] for key in request_keys if key in request.metadata
    }
    response_metadata.update(metadata or {})
    usage = usage or UsageInfo()
    return ModelEvent(
        type=ModelEventType.DONE,
        request_id=request.request_id,
        response=ModelResponse(
            request_id=request.request_id,
            model=request.model,
            provider=request.provider,
            blocks=tuple(blocks),
            finish_reason=finish,
            usage=usage,
            metadata=response_metadata,
        ),
    )


class AnthropicProvider:
    """ModelProvider adapter for the Anthropic Messages API."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = "https://api.anthropic.com",
        model: str = "claude-sonnet-4-5",
        provider: str = "anthropic",
        privacy_class: PrivacyClass = PrivacyClass.REMOTE,
        headers: dict[str, str] | None = None,
        timeout: float = 60.0,
        use_sdk: bool = True,
        cost: CostInfo | Mapping[str, object] | None = None,
        latency_class: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.provider = provider
        self._privacy_class = privacy_class
        if isinstance(cost, Mapping):
            cost = CostInfo(
                per_1m_input=_optional_float(cost.get("per_1m_input")),
                per_1m_output=_optional_float(cost.get("per_1m_output")),
                currency=str(cost.get("currency", "USD")),
            )
        self._cost = cost
        self._latency_class = latency_class
        self._api_key = api_key
        self._timeout = timeout
        self._headers = dict(headers or {})
        self._anthropic = _load_anthropic() if use_sdk else None
        self._client: httpx.AsyncClient | None = None
        self._active_streams: dict[str, httpx.Response] = {}

    def _http_client(self) -> httpx.AsyncClient:
        if self._client is None:
            if not self._api_key:
                raise ProviderAuthenticationError(
                    f"{self.provider} requires an api_key (no anthropic SDK available)"
                )
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout),
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": "2023-06-01",
                    **self._headers,
                },
            )
        return self._client

    async def list_models(self) -> list[ModelInfo]:
        return [
            ModelInfo(
                id=self.model,
                provider=self.provider,
                streaming=True,
                tool_calling=True,
                vision=True,
                reasoning=True,
                privacy_class=self._privacy_class,
                cost=self._cost,
                latency_class=self._latency_class,
            )
        ]

    async def complete(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        if self._anthropic is not None:
            yield await self._complete_sdk(request)
        else:
            async for event in self._complete_rest(request):
                yield event

    async def cancel(self, request_id: str) -> None:
        resp = self._active_streams.pop(request_id, None)
        if resp is not None:
            try:
                await resp.aclose()
            except (OSError, RuntimeError) as exc:
                _logger.debug("%s stream close failed: %s", self.provider, exc)

    async def _complete_sdk(self, request: ModelRequest) -> ModelEvent:
        assert self._anthropic is not None  # guarded by `complete` before dispatch
        client = self._anthropic.AsyncAnthropic(api_key=self._api_key, base_url=self.base_url)
        kwargs = self._build_kwargs(request, stream=False)
        response = await client.messages.create(**kwargs)
        blocks: list[ContentBlock] = []
        for content in response.content:
            if content.type == "text" and getattr(content, "text", None):
                blocks.append(TextBlock(type="text", text=content.text))
            elif content.type == "thinking" and getattr(content, "thinking", None):
                blocks.append(ReasoningBlock(type="reasoning", text=content.thinking))
            elif content.type == "tool_use":
                call_id = content.id or new_id("call")
                raw_input = json.dumps(
                    content.input if isinstance(content.input, dict) else {},
                    ensure_ascii=False,
                )
                if isinstance(content.input, str):
                    raw_input = content.input
                elif content.input is None:
                    raw_input = ""
                candidate = ToolCallCandidate.parse(
                    call_id,
                    content.name,
                    raw_input,
                    provider_profile_id=request.metadata.get("provider_profile_id"),
                    model_id=request.model,
                    protocol="anthropic",
                )
                blocks.append(
                    CapabilityCallBlock(
                        type="capability_call",
                        call_id=call_id,
                        capability_id=content.name,
                        arguments=candidate.parsed_arguments or {},
                        candidate=candidate,
                    )
                )
        usage = response.usage
        reported_cost = _reported_cost_usd(usage)
        if reported_cost is None:
            reported_cost = _reported_cost_usd(response)
        return _done_event(
            request,
            blocks=blocks,
            finish=response.stop_reason or "end_turn",
            usage=UsageInfo(
                input_tokens=getattr(usage, "input_tokens", 0) or 0,
                output_tokens=getattr(usage, "output_tokens", 0) or 0,
                cost_usd=reported_cost,
                cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
                cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
                provider_metadata={
                    "raw_usage": {
                        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
                        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
                        "cache_read_input_tokens": (
                            getattr(usage, "cache_read_input_tokens", 0) or 0
                        ),
                        "cache_creation_input_tokens": (
                            getattr(usage, "cache_creation_input_tokens", 0) or 0
                        ),
                    }
                },
            ),
            metadata=({"response_id": response.id} if getattr(response, "id", None) else None),
        )

    async def _complete_rest(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        payload = self._build_kwargs(request, stream=True)
        try:
            async with self._http_client().stream(
                "POST", self.base_url + _PATH, json=payload
            ) as resp:
                self._active_streams[request.request_id] = resp
                try:
                    if resp.status_code >= 400:
                        raise await self._map_err(resp)
                    if "event-stream" in resp.headers.get("content-type", "").lower():
                        async for event in self._iter_stream(request, resp):
                            yield event
                    else:
                        data = await self._read_json(resp)
                        yield self._parse_complete(request, data)
                finally:
                    self._active_streams.pop(request.request_id, None)
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(f"{self.provider} request timed out", cause=exc) from exc
        except httpx.RequestError as exc:
            raise ProviderUnavailable(f"{self.provider} unreachable: {exc}", cause=exc) from exc

    def _build_kwargs(self, request: ModelRequest, *, stream: bool) -> dict[str, Any]:
        system = self._system_prompt(request)
        kwargs: dict[str, Any] = {
            "model": request.model,
            "messages": self._translate_messages(request),
            "stream": stream,
        }
        if system:
            if request.metadata.get("cache_mode") in {"session-key", "explicit-cache-api"}:
                kwargs["system"] = [
                    {
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
            else:
                kwargs["system"] = system
        if request.max_tokens is not None:
            kwargs["max_tokens"] = request.max_tokens
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.stop:
            kwargs["stop_sequences"] = list(request.stop)
        if request.capabilities:
            kwargs["tools"] = [_tool_schema(b) for b in request.capabilities]
        return kwargs

    def _system_prompt(self, request: ModelRequest) -> str | None:
        if request.system:
            return request.system
        sys_msgs = [m for m in request.messages if m.role == Role.SYSTEM]
        texts = [m.text() for m in sys_msgs if m.text()]
        return "\n\n".join(texts) if texts else None

    def _translate_messages(self, request: ModelRequest) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for msg in request.messages:
            if msg.role == Role.SYSTEM:
                continue
            content: list[dict[str, Any]] = []
            for block in msg.blocks:
                if isinstance(block, (TextBlock, ReasoningBlock)) and block.text:
                    content.append({"type": "text", "text": block.text})
                elif isinstance(block, ImageBlock) and block.data_path:
                    content.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "url",
                                "url": block.data_path,
                            },
                        }
                    )
                elif isinstance(block, ArtifactRefBlock) and block.uri:
                    # Anthropic has no portable artifact-ref block. Keep the
                    # reference visible to the model instead of silently
                    # dropping durable context; callers that need native
                    # media should resolve it to an image/data URL first.
                    content.append(
                        {
                            "type": "text",
                            "text": f"[artifact attachment: {block.uri}]",
                        }
                    )
                elif isinstance(block, FileRefBlock) and block.uri:
                    content.append(
                        {
                            "type": "text",
                            "text": f"[file attachment: {block.uri}]",
                        }
                    )
                elif isinstance(block, AudioBlock) and block.data_path:
                    # Anthropic Messages currently has no canonical audio
                    # input block. Preserve the attachment explicitly so an
                    # unsupported request is observable rather than omitted.
                    content.append(
                        {
                            "type": "text",
                            "text": f"[audio attachment: {block.data_path}]",
                        }
                    )
                elif isinstance(block, CapabilityResultBlock):
                    result: dict[str, Any] = {
                        "type": "tool_result",
                        "tool_use_id": block.call_id,
                    }
                    if block.error:
                        result["content"] = [{"type": "text", "text": block.error}]
                        result["is_error"] = True
                    else:
                        result["content"] = [{"type": "text", "text": block.output}]
                    content.append(result)
                elif isinstance(block, CapabilityCallBlock):
                    content.append(
                        {
                            "type": "tool_use",
                            "id": block.call_id,
                            "name": block.capability_id,
                            "input": dict(block.arguments),
                        }
                    )
            role = "user" if msg.role in (Role.USER, Role.CAPABILITY) else "assistant"
            out.append({"role": role, "content": content or msg.text()})
        return out

    async def _read_json(self, resp: httpx.Response) -> dict[str, Any]:
        try:
            return resp.json()
        except ValueError as exc:
            raise ProviderMalformedResponse(
                f"{self.provider} returned non-JSON body", cause=exc
            ) from exc

    async def _iter_stream(
        self, request: ModelRequest, resp: httpx.Response
    ) -> AsyncIterator[ModelEvent]:
        tool_uses: dict[int, dict[str, Any]] = {}
        pending_tool_blocks: list[CapabilityCallBlock] = []
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        stream_complete = False
        async for line in resp.aiter_lines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            try:
                payload = json.loads(data)
            except ValueError:
                continue
            ptype = payload.get("type")
            if ptype == "content_block_start":
                block = payload.get("content_block") or {}
                if block.get("type") == "tool_use":
                    index = payload.get("index", 0)
                    tool_uses[index] = {
                        "call_id": block.get("id", ""),
                        "name": block.get("name", ""),
                        "input_tokens": [],
                        "input": {},
                    }
                elif block.get("type") == "thinking" and block.get("thinking"):
                    reasoning_parts.append(block["thinking"])
                    yield ModelEvent(
                        type=ModelEventType.REASONING,
                        request_id=request.request_id,
                        delta=ModelDelta(
                            request_id=request.request_id,
                            reasoning=block["thinking"],
                        ),
                    )
            elif ptype == "content_block_delta":
                delta = payload.get("delta") or {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    text_parts.append(delta["text"])
                    yield ModelEvent(
                        type=ModelEventType.DELTA,
                        request_id=request.request_id,
                        delta=ModelDelta(request_id=request.request_id, text=delta["text"]),
                    )
                elif delta.get("type") == "thinking_delta" and delta.get("thinking"):
                    reasoning_parts.append(delta["thinking"])
                    yield ModelEvent(
                        type=ModelEventType.REASONING,
                        request_id=request.request_id,
                        delta=ModelDelta(
                            request_id=request.request_id,
                            reasoning=delta["thinking"],
                        ),
                    )
                elif delta.get("type") == "input_json_delta":
                    index = payload.get("index", 0)
                    slot = tool_uses.get(index)
                    if slot is not None and delta.get("partial_json"):
                        slot["input_tokens"].append(delta["partial_json"])
            elif ptype == "content_block_stop":
                index = payload.get("index", 0)
                slot = tool_uses.get(index)
                if slot is not None:
                    call_id = slot["call_id"] or new_id("call")
                    # An empty/incomplete stream is still a candidate.  Keep
                    # the original empty payload so the compatibility layer
                    # can reject or repair it; manufacturing ``{}`` turns a
                    # malformed model call into a valid one silently.
                    raw_input = "".join(slot["input_tokens"])
                    candidate = ToolCallCandidate.parse(
                        call_id,
                        slot["name"],
                        raw_input,
                        completion_state="CLEAN" if stream_complete else "INTERRUPTED",
                        provider_profile_id=request.metadata.get("provider_profile_id"),
                        model_id=request.model,
                        stream="anthropic",
                    )
                    block = CapabilityCallBlock(
                        type="capability_call",
                        call_id=call_id,
                        capability_id=slot["name"],
                        arguments=candidate.parsed_arguments or {},
                        candidate=candidate,
                    )
                    pending_tool_blocks.append(block)
            elif ptype == "error":
                raise ProviderProtocolError(f"{self.provider} stream error: {payload}")
            elif ptype == "message_stop":
                stream_complete = True
        for block in pending_tool_blocks:
            candidate = block.candidate
            if candidate is not None:
                candidate = replace(
                    candidate,
                    completion_state="CLEAN" if stream_complete else "INTERRUPTED",
                )
                if candidate.parsed_arguments is None:
                    record_raw_candidate(candidate)
                block = replace(block, candidate=candidate)
            yield ModelEvent(
                type=ModelEventType.DELTA,
                request_id=request.request_id,
                delta=ModelDelta(request_id=request.request_id, text="", block=block),
            )
        blocks: list[ContentBlock] = []
        if reasoning_parts:
            blocks.append(ReasoningBlock(type="reasoning", text="".join(reasoning_parts)))
        if text_parts:
            blocks.append(TextBlock(type="text", text="".join(text_parts)))
        yield _done_event(request, blocks=blocks)

    def _parse_complete(self, request: ModelRequest, data: dict[str, Any]) -> ModelEvent:
        blocks: list[ContentBlock] = []
        for content in data.get("content") or []:
            if content.get("type") == "text" and content.get("text"):
                blocks.append(TextBlock(type="text", text=content["text"]))
            elif content.get("type") == "thinking" and content.get("thinking"):
                blocks.append(ReasoningBlock(type="reasoning", text=content["thinking"]))
            elif content.get("type") == "tool_use":
                call_id = content.get("id") or new_id("call")
                raw_input = content.get("input")
                if isinstance(raw_input, dict):
                    raw_arguments = json.dumps(raw_input, ensure_ascii=False)
                elif isinstance(raw_input, str):
                    raw_arguments = raw_input
                else:
                    # Preserve a missing/non-object payload as invalid raw
                    # input.  ``{}`` is a real empty-object argument and is
                    # not an acceptable parse-failure sentinel.
                    raw_arguments = ""
                candidate = ToolCallCandidate.parse(
                    call_id,
                    content.get("name", ""),
                    raw_arguments,
                    provider_profile_id=request.metadata.get("provider_profile_id"),
                    model_id=request.model,
                    protocol="anthropic",
                )
                if candidate.parsed_arguments is None:
                    record_raw_candidate(candidate)
                blocks.append(
                    CapabilityCallBlock(
                        type="capability_call",
                        call_id=call_id,
                        capability_id=content.get("name", ""),
                        arguments=candidate.parsed_arguments or {},
                        candidate=candidate,
                    )
                )
        usage_raw = data.get("usage") or {}
        reported_cost = _reported_cost_usd(usage_raw)
        if reported_cost is None:
            reported_cost = _reported_cost_usd(data)
        return _done_event(
            request,
            blocks=blocks,
            finish=data.get("stop_reason") or "end_turn",
            usage=UsageInfo(
                input_tokens=int(usage_raw.get("input_tokens") or 0),
                output_tokens=int(usage_raw.get("output_tokens") or 0),
                cost_usd=reported_cost,
                cache_read_tokens=int(usage_raw.get("cache_read_input_tokens") or 0),
                cache_write_tokens=int(usage_raw.get("cache_creation_input_tokens") or 0),
                provider_metadata={"raw_usage": usage_raw},
            ),
            metadata={"response_id": data.get("id")} if data.get("id") else {},
        )

    async def _map_err(self, resp: httpx.Response):
        code = resp.status_code
        body = (await resp.aread()).decode("utf-8", errors="replace")
        message = f"{self.provider} http {code}: {body[:200]}"
        if code in (401, 403):
            return ProviderAuthenticationError(message)
        if code == 429:
            return ProviderRateLimitError(message)
        if code == 400 and ("context" in body.lower() or "token" in body.lower()):
            return ContextOverflow(message)
        if code == 404:
            return ModelUnavailable(message)
        if 500 <= code < 600:
            return ProviderUnavailable(message)
        return ProviderProtocolError(message)


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


__all__ = ["AnthropicProvider"]
