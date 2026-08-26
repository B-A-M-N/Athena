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
from typing import Any, AsyncIterator

import httpx

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
from athena.models.compat.candidates import ToolCallCandidate, record_raw_candidate
from athena.protocol.messages import (
    CapabilityCallBlock,
    CapabilityResultBlock,
    ContentBlock,
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
    PrivacyClass,
    UsageInfo,
)

_PATH = "/v1/messages"


def _load_anthropic():
    try:
        import anthropic  # type: ignore

        return anthropic
    except Exception:
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
    usage: UsageInfo = UsageInfo(),
) -> ModelEvent:
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
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.provider = provider
        self._privacy_class = privacy_class
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
                privacy_class=self._privacy_class,
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
            except Exception:
                pass

    async def _complete_sdk(self, request: ModelRequest) -> ModelEvent:
        assert self._anthropic is not None  # guarded by `complete` before dispatch
        client = self._anthropic.AsyncAnthropic(api_key=self._api_key, base_url=self.base_url)
        kwargs = self._build_kwargs(request, stream=False)
        response = await client.messages.create(**kwargs)
        blocks: list[ContentBlock] = []
        for content in response.content:
            if content.type == "text" and getattr(content, "text", None):
                blocks.append(TextBlock(type="text", text=content.text))
            elif content.type == "tool_use":
                blocks.append(
                    CapabilityCallBlock(
                        type="capability_call",
                        call_id=content.id,
                        capability_id=content.name,
                        arguments=content.input,
                    )
                )
        usage = response.usage
        return _done_event(
            request,
            blocks=blocks,
            finish=response.stop_reason or "end_turn",
            usage=UsageInfo(
                input_tokens=getattr(usage, "input_tokens", 0) or 0,
                output_tokens=getattr(usage, "output_tokens", 0) or 0,
            ),
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
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
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
            elif ptype == "content_block_delta":
                delta = payload.get("delta") or {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    text_parts.append(delta["text"])
                    yield ModelEvent(
                        type=ModelEventType.DELTA,
                        request_id=request.request_id,
                        delta=ModelDelta(
                            request_id=request.request_id, text=delta["text"]
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
                    raw_input = "".join(slot["input_tokens"]) or "{}"
                    try:
                        arguments = json.loads(raw_input)
                    except ValueError:
                        arguments = {}
                    if not isinstance(arguments, dict):
                        arguments = {}
                    if arguments == {} and raw_input.strip() not in ("", "{}"):
                        candidate = ToolCallCandidate.parse(
                            slot["call_id"] or new_id("call"),
                            slot["name"],
                            raw_input,
                        )
                        if candidate.parsed_arguments is None:
                            record_raw_candidate(candidate)
                    block = CapabilityCallBlock(
                        type="capability_call",
                        call_id=slot["call_id"] or new_id("call"),
                        capability_id=slot["name"],
                        arguments=arguments,
                    )
                    yield ModelEvent(
                        type=ModelEventType.DELTA,
                        request_id=request.request_id,
                        delta=ModelDelta(request_id=request.request_id, text="", block=block),
                    )
            elif ptype == "error":
                raise ProviderProtocolError(f"{self.provider} stream error: {payload}")
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
            elif content.get("type") == "tool_use":
                blocks.append(
                    CapabilityCallBlock(
                        type="capability_call",
                        call_id=content.get("id") or new_id("call"),
                        capability_id=content.get("name", ""),
                        arguments=content.get("input") or {},
                    )
                )
        usage_raw = data.get("usage") or {}
        return _done_event(
            request,
            blocks=blocks,
            finish=data.get("stop_reason") or "end_turn",
            usage=UsageInfo(
                input_tokens=int(usage_raw.get("input_tokens") or 0),
                output_tokens=int(usage_raw.get("output_tokens") or 0),
            ),
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


__all__ = ["AnthropicProvider"]