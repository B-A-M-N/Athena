from __future__ import annotations

import httpx
import pytest

from athena.models.providers.openai_compat import OpenAICompatProvider


@pytest.mark.asyncio
async def test_openai_compatible_voice_endpoints_use_provider_base_url():
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/transcriptions"):
            return httpx.Response(200, json={"text": "hello", "language": "en"})
        return httpx.Response(200, content=b"speech")

    provider = OpenAICompatProvider(
        base_url="https://voice.example/v1",
        api_key="secret",
        model="chat",
        provider="voice",
    )
    await provider._client.aclose()
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        transcript = await provider.transcribe_audio(
            b"wav",
            filename="voice.wav",
            mime_type="audio/wav",
            model="whisper-1",
        )
        audio = await provider.synthesize_audio(
            "hello",
            model="tts-1",
            voice="alloy",
            response_format="mp3",
        )
    finally:
        await provider._client.aclose()

    assert transcript == {"text": "hello", "language": "en"}
    assert audio == b"speech"
    assert [request.url.path for request in seen] == [
        "/v1/audio/transcriptions",
        "/v1/audio/speech",
    ]
