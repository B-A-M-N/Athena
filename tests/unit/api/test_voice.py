from __future__ import annotations

import base64
from dataclasses import dataclass, field
from types import SimpleNamespace

import httpx
import pytest

from athena.api.app import create_app
from athena.artifacts.store import ArtifactStore
from athena.protocol.artifacts import ArtifactRef
from athena.service.config import AthenaConfig, VoiceConfig
from athena.voice import VoiceSynthesis, VoiceTranscript


@dataclass
class _VoiceService:
    root: object
    config: AthenaConfig = field(
        default_factory=lambda: AthenaConfig(
            voice=VoiceConfig(
                enabled=True,
                transcription_provider="voice",
                synthesis_provider="voice",
            )
        )
    )
    _started: bool = True
    submitted: list[object] = field(default_factory=list)

    @property
    def _artifacts(self):
        return self.root

    def voice_health(self):
        return {"enabled": True, "state": "ready"}

    async def transcribe_voice(self, data, **kwargs):
        assert data == b"audio"
        return VoiceTranscript(
            text="inspect the repository",
            artifact=ArtifactRef(
                id="artifact://sha256/input",
                uri="artifact://sha256/input",
                hash="input",
                mime_type="audio/wav",
                size=5,
            ),
            provider="voice",
            model="whisper-1",
            language="en",
        )

    async def submit(self, request, *, wait=False):
        assert wait is False
        self.submitted.append(request)
        return SimpleNamespace(id="task-1", session_id=request.session_id)

    async def synthesize_voice(self, text, **kwargs):
        assert text == "hello"
        ref = await self.root.save(content=b"speech", mime_type="audio/mpeg")
        return VoiceSynthesis(
            artifact=ref,
            provider="voice",
            model="tts-1",
            voice="alloy",
            response_format="mp3",
        )


@pytest.mark.asyncio
async def test_voice_turn_transcribes_then_submits_a_normal_task(tmp_path):
    service = _VoiceService(ArtifactStore(tmp_path / "artifacts"))
    app = create_app(service)
    transport = httpx.ASGITransport(app=app)
    encoded = base64.b64encode(b"audio").decode("ascii")
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/voice/turn",
            json={"audio_base64": encoded, "mime_type": "audio/wav", "session_id": "s-1"},
        )

    assert response.status_code == 202
    assert response.json()["task_id"] == "task-1"
    assert service.submitted[0].prompt == "inspect the repository"
    assert service.submitted[0].attachments[0]["kind"] == "voice"


@pytest.mark.asyncio
async def test_voice_synthesis_returns_audio_and_artifact_header(tmp_path):
    service = _VoiceService(ArtifactStore(tmp_path / "artifacts"))
    app = create_app(service)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/v1/voice/synthesize", json={"text": "hello"})

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.content == b"speech"
    assert response.headers["x-athena-artifact-uri"].startswith("artifact://sha256/")


@pytest.mark.asyncio
async def test_task_steering_is_queued_at_api_boundary(tmp_path):
    service = _VoiceService(ArtifactStore(tmp_path / "artifacts"))
    service.steer_task = _steer_task
    app = create_app(service)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/tasks/task-1/steer",
            json={"text": "use the safer fallback", "source_task_id": "parent-1"},
        )

    assert response.status_code == 202
    assert response.json()["steering"]["task_id"] == "task-1"


async def _steer_task(task_id, text, *, source_task_id=None):
    return {
        "id": "steer-1",
        "task_id": task_id,
        "text": text,
        "source_task_id": source_task_id,
        "status": "PENDING",
    }
