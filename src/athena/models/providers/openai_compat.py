"""OpenAI-compatible chat/completions provider adapter.

Covers any server that speaks the OpenAI chat-completions wire shape (OpenAI,
Together, Groq, local vLLM/llama.cpp, ...) by setting a base URL + key. All
provider-specific branching is confined to this adapter (INV-006). Input and
output remain the canonical provider-neutral ModelRequest/ModelEvent shapes.
Streaming is native via ``complete``; a consumer accumulates events when it
wants a whole response.
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Mapping
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
    Message,
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

_logger = logging.getLogger("athena.provider.openai_compat")

_PATH = "/chat/completions"

_ROLE_MAP: dict[Role, str] = {
    Role.USER: "user",
    Role.ASSISTANT: "assistant",
    Role.CAPABILITY: "tool",
    Role.SYSTEM: "system",
}


def _capability_schema(desc) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": desc.id,
            "description": desc.description or f"Athena capability {desc.id}",
            "parameters": desc.input_schema or {"type": "object", "properties": {}},
        },
    }


def parse_tool_arguments(raw: str | dict) -> dict[str, Any] | None:
    """Parse model-produced tool arguments without destroying failures.

    ``None`` means the provider payload was not a JSON object.  The caller
    must retain the original bytes in ``ToolCallCandidate``; an empty object
    is a valid value and must never be used as a lossy parse-failure marker.
    This function is intentionally separate from ``serialize_tool_result``:
    tool results are model-visible content, not tool-input JSON.
    """
    if isinstance(raw, dict):
        return dict(raw)
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except ValueError:
        return None


def _emit_candidate(
    call_id: str,
    capability_id: str,
    raw: str | dict,
    **metadata: Any,
) -> ToolCallCandidate:
    """Preserve the RAW arguments string at the boundary when the canonical
    parse fails, so the repair engine can act on original bytes."""
    if isinstance(raw, dict):
        return ToolCallCandidate(
            call_id=call_id,
            capability_id=capability_id,
            raw_arguments=json.dumps(raw, ensure_ascii=False),
            parsed_arguments=dict(raw),
            provider_profile_id=metadata.get("provider_profile_id"),
            model_id=metadata.get("model_id"),
            provider_metadata={k: v for k, v in metadata.items()
                               if k not in {"provider_profile_id", "model_id"}},
        )
    candidate = ToolCallCandidate.parse(
        call_id, capability_id, raw, **metadata)
    if candidate.parsed_arguments is None:
        record_raw_candidate(candidate)
    return candidate


def serialize_tool_result(output: str | None) -> str:
    """Serialize tool RESULT output as model-visible CONTENT.

    Tool output is arbitrary text (stdout, summaries, errors), not JSON.
    It must reach the next request verbatim — never round-tripped through
    a JSON parser that would collapse ordinary text into {}.
    """
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    return json.dumps(output, ensure_ascii=False, default=str)


def _response_metadata(
    request: ModelRequest, *, extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Carry request provenance/cache identity onto every provider response."""
    keys = (
        "task_id", "session_id", "provider_profile_id",
        "provider_profile_fingerprint", "profile_id", "model_id",
        "compatibility_profile", "model_profile", "protocol",
        "tool_repair_mode", "max_tool_correction_cycles", "cache_mode",
        "cache_session_key", "prefix_fingerprint", "full_fingerprint",
        "components_fp",
    )
    result = {key: request.metadata[key] for key in keys if key in request.metadata}
    result.update(extra or {})
    return result


class OpenAICompatProvider:
    """ModelProvider adapter against the OpenAI chat/completions wire format."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str = "gpt-4o-mini",
        provider: str = "openai-compat",
        privacy_class: PrivacyClass = PrivacyClass.REMOTE,
        headers: Mapping[str, str] | None = None,
        timeout: float = 60.0,
        http2: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.provider = provider
        self._privacy_class = privacy_class
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            http2=http2,
            headers={"Authorization": f"Bearer {api_key}", **(headers or {})},
        )
        # P2-67: per-instance stream tracking — multiple configured
        # OpenAI-compatible routes must not share cancellation bookkeeping.
        self._active_streams: dict[str, httpx.Response] = {}

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
        payload = self._build_request(request)
        try:
            async with self._client.stream("POST", self.base_url + _PATH, json=payload) as resp:
                if resp.status_code >= 400:
                    raise await self._map_err(resp)
                # Register active stream for cancellation
                self._active_streams[request.request_id] = resp
                try:
                    if "text/event-stream" in resp.headers.get("content-type", "").lower():
                        async for event in self._stream_events(request, resp):
                            yield event
                    else:
                        data = await self._read_json(resp)
                        yield self._parse_complete(request, data)
                finally:
                    # Unregister stream when done
                    self._active_streams.pop(request.request_id, None)
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(f"{self.provider} request timed out", cause=exc) from exc
        except httpx.RequestError as exc:
            raise ProviderUnavailable(f"{self.provider} unreachable: {exc}", cause=exc) from exc

    async def cancel(self, request_id: str) -> None:
        """Cancel an in-progress stream by closing the active HTTP response."""
        resp = self._active_streams.get(request_id)
        if resp is not None:
            _logger.info("cancelling stream for request %s", request_id)
            await resp.aclose()
            self._active_streams.pop(request_id, None)
        else:
            _logger.info("cancel requested for request %s (no active stream)", request_id)

    # -- translation (provider-specific shape lives only here) -----------------
    def _build_request(self, request: ModelRequest) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        for msg in request.messages:
            translated = self._translate_message(msg)
            if isinstance(translated, list):
                messages.extend(translated)
            else:
                messages.append(translated)

        payload: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "stream": True,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.stop:
            payload["stop"] = list(request.stop)
        tools = [_capability_schema(b) for b in request.capabilities]
        if tools:
            payload["tools"] = tools
        # OpenAI's cache key is a request-semantic control.  Do not send it to
        # generic local servers unless the resolved profile explicitly says it
        # speaks the OpenAI cache extension.
        if (
            request.metadata.get("protocol") == "openai"
            and request.metadata.get("cache_session_key")
            and request.metadata.get("cache_mode")
            in {"automatic-prefix", "explicit-cache-api"}
        ):
            payload["prompt_cache_key"] = request.metadata["cache_session_key"]
        return payload

    def _translate_message(self, msg: Message) -> list[dict[str, Any]] | dict[str, Any]:
        role = _ROLE_MAP.get(msg.role, "user")
        if role == "tool":
            results = [
                b for b in msg.blocks if isinstance(b, CapabilityResultBlock)
            ]
            if results:
                return [
                    {
                        "role": "tool",
                        "content": serialize_tool_result(
                            b.output if b.output else b.error
                        ),
                        "tool_call_id": b.call_id,
                    }
                    for b in results
                ]
            return {"role": "tool", "content": msg.text()}
        text = "\n".join(
            b.text for b in msg.blocks
            if isinstance(b, (TextBlock, ReasoningBlock)) and b.text
        )
        content_parts: list[dict[str, Any]] = []
        if text:
            content_parts.append({"type": "text", "text": text})
        for block in msg.blocks:
            if isinstance(block, ImageBlock) and block.data_path:
                content_parts.append({
                    "type": "image_url",
                    "image_url": {"url": block.data_path},
                })
            elif isinstance(block, AudioBlock) and block.data_path:
                audio_format = (block.mime_type or "audio/wav").split("/", 1)[-1]
                content_parts.append({
                    "type": "input_audio",
                    "input_audio": {
                        "data": block.data_path,
                        "format": audio_format,
                    },
                })
            elif isinstance(block, (ArtifactRefBlock, FileRefBlock)) and block.uri:
                # The generic chat-completions protocol has no portable
                # artifact part. Preserve the reference as an explicit file
                # part rather than silently dropping the attachment.
                content_parts.append({
                    "type": "text",
                    "text": f"[artifact attachment: {block.uri}]",
                })
        content: str | list[dict[str, Any]] = text
        if content_parts and not (
            len(content_parts) == 1
            and content_parts[0]["type"] == "text"
            and content_parts[0]["text"] == text
        ):
            content = content_parts
        payload: dict[str, Any] = {"role": role, "content": content}
        if role == "assistant":
            calls = [
                {
                    "id": b.call_id or new_id("call"),
                    "type": "function",
                    "function": {
                        "name": b.capability_id,
                        "arguments": (
                            b.candidate.raw_arguments
                            if b.candidate is not None
                            and isinstance(b.candidate.raw_arguments, str)
                            else json.dumps(dict(b.arguments), ensure_ascii=False)
                        ),
                    },
                }
                for b in msg.blocks
                if isinstance(b, CapabilityCallBlock)
            ]
            if calls:
                payload["tool_calls"] = calls
        return payload

    async def _read_json(self, resp: httpx.Response) -> dict[str, Any]:
        try:
            return resp.json()
        except ValueError as exc:
            raise ProviderMalformedResponse(
                f"{self.provider} returned non-JSON body", cause=exc
            ) from exc

    async def _stream_events(
        self, request: ModelRequest, resp: httpx.Response
    ) -> AsyncIterator[ModelEvent]:
        tool_calls: dict[int, dict[str, str]] = {}
        usage = UsageInfo()
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        stream_complete = False
        async for line in resp.aiter_lines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                stream_complete = True
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                continue
            usage_raw = chunk.get("usage")
            if usage_raw:
                details = usage_raw.get("prompt_tokens_details") or {}
                cached = int(details.get("cached_tokens") or 0)
                prompt = int(usage_raw.get("prompt_tokens") or 0)
                usage = UsageInfo(
                    input_tokens=prompt,
                    output_tokens=int(usage_raw.get("completion_tokens") or 0),
                    reasoning_tokens=int(usage_raw.get("reasoning_tokens") or 0),
                    cache_read_tokens=cached,
                    uncached_input_tokens=max(prompt - cached, 0),
                    provider_metadata={"raw_usage": usage_raw},
                )
            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            if delta.get("reasoning_content"):
                reasoning_parts.append(delta["reasoning_content"])
                yield ModelEvent(
                    type=ModelEventType.REASONING,
                    request_id=request.request_id,
                    delta=ModelDelta(
                        request_id=request.request_id,
                        reasoning=delta["reasoning_content"],
                    ),
                )
            if delta.get("content"):
                text_parts.append(delta["content"])
                yield ModelEvent(
                    type=ModelEventType.DELTA,
                    request_id=request.request_id,
                    delta=ModelDelta(
                        request_id=request.request_id, text=delta["content"]
                    ),
                )
            for tc in delta.get("tool_calls") or []:
                self._accumulate_tool_call(tc, tool_calls)
        for call in tool_calls.values():
            candidate = _emit_candidate(
                call["call_id"], call["name"], call["arguments"],
                completion_state="CLEAN" if stream_complete else "INTERRUPTED",
                provider_profile_id=request.metadata.get("provider_profile_id"),
                model_id=request.model,
                stream="openai-compat",
            )
            yield ModelEvent(
                type=ModelEventType.DELTA,
                request_id=request.request_id,
                delta=ModelDelta(
                    request_id=request.request_id,
                    text="",
                    block=CapabilityCallBlock(
                        type="capability_call",
                        call_id=call["call_id"],
                        capability_id=call["name"],
                        arguments=candidate.parsed_arguments or {},
                        candidate=candidate,
                    ),
                ),
            )
        blocks: list[ContentBlock] = []
        if reasoning_parts:
            blocks.append(ReasoningBlock(type="reasoning", text="".join(reasoning_parts)))
        if text_parts:
            blocks.append(TextBlock(type="text", text="".join(text_parts)))
        yield ModelEvent(
            type=ModelEventType.DONE,
            request_id=request.request_id,
            response=ModelResponse(
                request_id=request.request_id,
                model=request.model,
                provider=request.provider,
                blocks=tuple(blocks),
                finish_reason="stop",
                usage=usage,
                metadata=_response_metadata(request),
            ),
        )

    def _accumulate_tool_call(
        self, tc: dict[str, Any], tool_calls: dict[int, dict[str, str]]
    ) -> None:
        index = tc.get("index", 0)
        fn = tc.get("function") or {}
        slot = tool_calls.setdefault(index, {"call_id": "", "name": "", "arguments": ""})
        if tc.get("id"):
            slot["call_id"] = tc["id"]
        if fn.get("name"):
            slot["name"] = fn["name"]
        if fn.get("arguments"):
            slot["arguments"] += fn["arguments"]

    def _parse_complete(self, request: ModelRequest, data: dict[str, Any]) -> ModelEvent:
        choices = data.get("choices") or []
        finish = "stop"
        blocks: list[ContentBlock] = []
        if choices:
            finish = choices[0].get("finish_reason") or "stop"
            message = choices[0].get("message") or {}
            reasoning = message.get("reasoning_content") or message.get("reasoning")
            if isinstance(reasoning, str) and reasoning:
                blocks.append(ReasoningBlock(type="reasoning", text=reasoning))
            if message.get("content"):
                blocks.append(TextBlock(type="text", text=message["content"]))
            for tc in message.get("tool_calls") or []:
                fn = tc.get("function") or {}
                call_id = tc.get("id") or new_id("call")
                raw_arguments = fn.get("arguments")
                if not isinstance(raw_arguments, str):
                    raw_arguments = ""
                candidate = _emit_candidate(
                    call_id,
                    fn.get("name", ""),
                    raw_arguments,
                    provider_profile_id=request.metadata.get("provider_profile_id"),
                    model_id=request.model,
                    stream="openai-compat",
                )
                blocks.append(
                    CapabilityCallBlock(
                        type="capability_call",
                        call_id=call_id,
                        capability_id=fn.get("name", ""),
                        arguments=candidate.parsed_arguments or {},
                        candidate=candidate,
                    )
                )
        usage_raw = data.get("usage") or {}
        details = usage_raw.get("prompt_tokens_details") or {}
        cached = int(details.get("cached_tokens") or 0)
        prompt = int(usage_raw.get("prompt_tokens") or 0)
        usage = UsageInfo(
            input_tokens=prompt,
            output_tokens=int(usage_raw.get("completion_tokens") or 0),
            cache_read_tokens=cached,
            uncached_input_tokens=max(prompt - cached, 0),
            provider_metadata={"raw_usage": usage_raw},
        )
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
                metadata=_response_metadata(
                    request,
                    extra={"response_id": data["id"]} if data.get("id") else None,
                ),
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
        if code == 400 and ("context" in body.lower() or "max tokens" in body.lower()):
            return ContextOverflow(message)
        if code == 404:
            return ModelUnavailable(message)
        if 500 <= code < 600:
            return ProviderUnavailable(message)
        return ProviderProtocolError(message)


__all__ = ["OpenAICompatProvider", "parse_tool_arguments",
           "serialize_tool_result"]
